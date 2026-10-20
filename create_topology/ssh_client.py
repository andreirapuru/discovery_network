from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time


class DeviceConnectionError(Exception):
    """Raised when a device cannot be reached or authenticated."""

    def __init__(self, message: str, category: str = "connection") -> None:
        super().__init__(message)
        self.category = category


@dataclass(slots=True)
class CommandOutputs:
    show_version: str
    show_inventory: str
    show_cdp_neighbors_detail: str
    show_lldp_neighbors_detail: str
    show_interfaces_trunk: str
    show_interface_switchport: str
    command_errors: dict[str, str] | None = None


class CiscoSSHClient:
    def __init__(
        self,
        username: str,
        password: str,
        secret: str,
        timeout: int = 10,
        jump_host: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.secret = secret
        self.timeout = timeout
        self.jump_host = jump_host
        self.logger = logger
        self._jump_connection = None
        self._seed_prompt = ""

    def collect(self, ip: str) -> CommandOutputs:
        if self.jump_host:
            if ip == self.jump_host:
                return self._collect_seed()
            return self._collect_via_seed(ip)
        return self._collect_direct(ip)

    def close(self) -> None:
        if self._jump_connection is not None:
            try:
                self._jump_connection.disconnect()
            finally:
                self._jump_connection = None

    def _info(self, message: str, *args) -> None:
        if self.logger:
            self.logger.info("[SSH] " + message, *args)

    def _device_parameters(self, host: str) -> dict[str, object]:
        return {
            "device_type": "cisco_ios",
            "host": host,
            "username": self.username,
            "password": self.password,
            "secret": self.secret,
            "timeout": self.timeout,
            "fast_cli": False,
        }

    def _collect_direct(self, ip: str) -> CommandOutputs:
        try:
            from netmiko import ConnectHandler
            from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
        except ImportError as exc:
            raise DeviceConnectionError("Netmiko is not installed.") from exc

        try:
            self._info("Opening direct connection to %s", ip)
            with ConnectHandler(**self._device_parameters(ip)) as connection:
                return self._collect_commands(connection)
        except DeviceConnectionError:
            raise
        except NetmikoAuthenticationException as exc:
            raise DeviceConnectionError(str(exc), "authentication") from exc
        except NetmikoTimeoutException as exc:
            raise DeviceConnectionError(str(exc), "timeout") from exc
        except Exception as exc:  # pragma: no cover - vendor CLI variance
            message = str(exc)
            if "no acceptable kex algorithm" in message.lower():
                message = (
                    f"{message} Legacy SSH algorithms are required by this device. "
                    "Install the pinned project requirements to use a compatible Paramiko version."
                )
            raise DeviceConnectionError(message) from exc

    def _get_jump_connection(self):
        if self._jump_connection is not None:
            return self._jump_connection
        try:
            from netmiko import ConnectHandler
        except ImportError as exc:
            raise DeviceConnectionError("Netmiko is not installed.") from exc
        self._jump_connection = ConnectHandler(**self._device_parameters(self.jump_host))
        self._seed_prompt = self._jump_connection.base_prompt
        self._info("Seed session established: %s (%s)", self.jump_host, self._seed_prompt)
        return self._jump_connection

    def _collect_seed(self) -> CommandOutputs:
        try:
            return self._collect_commands(self._get_jump_connection())
        except DeviceConnectionError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise DeviceConnectionError(f"Seed connection failed: {exc}") from exc

    def _collect_via_seed(self, ip: str) -> CommandOutputs:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", self.username):
            raise DeviceConnectionError(
                "Username contains characters unsupported by the seed SSH command.", "jump"
            )
        try:
            from netmiko import redispatch
            from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
        except ImportError as exc:
            raise DeviceConnectionError("Netmiko is not installed.") from exc

        try:
            connection = self._get_jump_connection()
            nested_ready = False
            try:
                self._info("Opening neighbor %s through seed %s", ip, self.jump_host)
                transcript = self._open_nested_ssh(connection, ip)
                redispatch(connection, device_type="cisco_ios", session_prep=False)
                connection.set_base_prompt()
                nested_ready = True
                self._info("Neighbor session established: %s (%s)", ip, connection.base_prompt)
                try:
                    connection.disable_paging(command="terminal length 0")
                except Exception:
                    pass
                return self._collect_commands(connection)
            finally:
                self._return_to_seed(connection, redispatch, nested_ready)
        except DeviceConnectionError:
            raise
        except NetmikoAuthenticationException as exc:
            self.close()
            raise DeviceConnectionError(
                f"Authentication failed while using seed {self.jump_host}: {exc}", "authentication"
            ) from exc
        except NetmikoTimeoutException as exc:
            self.close()
            raise DeviceConnectionError(
                f"Timeout while using seed {self.jump_host} to reach {ip}: {exc}", "timeout"
            ) from exc
        except Exception as exc:
            self.close()
            raise DeviceConnectionError(
                f"Seed {self.jump_host} could not reach {ip}: {exc}", "jump"
            ) from exc

    def _return_to_seed(self, connection, redispatch, nested_ready: bool) -> None:
        try:
            if nested_ready:
                self._info("Returning from neighbor to seed %s", self.jump_host)
                connection.write_channel("exit\n")
            else:
                # Cisco's SSH escape sequence returns to the seed even when
                # the nested login is incomplete.
                connection.write_channel("\x1e")
                time.sleep(0.2)
                connection.write_channel("x")
            prompt_output = self._read_until_seed_prompt(connection)
            if self._seed_prompt.lower() not in prompt_output.lower():
                raise RuntimeError("seed prompt was not restored after nested SSH")
            redispatch(connection, device_type="cisco_ios", session_prep=False)
            connection.set_base_prompt()
            if connection.base_prompt.lower() != self._seed_prompt.lower():
                raise RuntimeError(
                    f"expected seed prompt {self._seed_prompt!r}, got {connection.base_prompt!r}"
                )
        except Exception:
            self.close()

    def _read_until_seed_prompt(self, connection, timeout: int = 10) -> str:
        output = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            output += connection.read_channel()
            if self._seed_prompt and re.search(
                rf"{re.escape(self._seed_prompt)}(?:#|>)\s*$", output, re.IGNORECASE
            ):
                return output
        return output

    def _open_nested_ssh(self, connection, ip: str, timeout: int = 40) -> str:
        transcript = ""
        handled_yes = False
        handled_username = False
        handled_passwords = 0
        deadline = time.monotonic() + timeout
        connection.write_channel(f"ssh -l {self.username} {ip}\n")

        while time.monotonic() < deadline:
            time.sleep(0.5)
            chunk = connection.read_channel()
            if not chunk:
                continue
            transcript += chunk
            lowered = transcript.lower()

            failure_markers = (
                "permission denied",
                "authentication failed",
                "connection refused",
                "connection timed out",
                "no route to host",
                "unknown host",
                "% ssh",
            )
            if any(marker in lowered for marker in failure_markers):
                detail = " ".join(transcript[-500:].split())
                raise DeviceConnectionError(
                    f"Seed {self.jump_host} could not open SSH to {ip}: {detail}", "jump"
                )

            if not handled_yes and ("yes/no" in lowered or "continue connecting" in lowered):
                connection.write_channel("yes\n")
                handled_yes = True
                continue

            username_prompts = lowered.count("username:")
            if username_prompts and not handled_username:
                connection.write_channel(self.username + "\n")
                handled_username = True
                continue

            password_prompts = lowered.count("password:")
            if password_prompts > handled_passwords:
                if handled_passwords:
                    raise DeviceConnectionError(
                        f"Authentication failed on neighbor {ip}: password was requested again.",
                        "authentication",
                    )
                connection.write_channel(self.password + "\n")
                handled_passwords = password_prompts
                continue

            prompt_match = re.search(r"([^\r\n]+?)(?:#|>)\s*$", transcript)
            if prompt_match:
                prompt_name = prompt_match.group(1).strip()
                if self._seed_prompt and prompt_name.casefold() == self._seed_prompt.casefold():
                    detail = " ".join(transcript[-500:].split())
                    raise DeviceConnectionError(
                        f"SSH to neighbor {ip} returned to the seed prompt without opening "
                        f"a device session: {detail}",
                        "jump",
                    )
                return transcript

        detail = " ".join(transcript[-500:].split())
        raise DeviceConnectionError(
            f"Neighbor {ip} did not present a Cisco prompt within {timeout}s: {detail}", "jump"
        )

    def _collect_commands(self, connection) -> CommandOutputs:
        self._info("Entering privileged mode on %s", getattr(connection, "base_prompt", "device"))
        try:
            connection.enable()
            if hasattr(connection, "check_enable_mode") and not connection.check_enable_mode():
                raise RuntimeError("privileged EXEC mode was not reached")
        except Exception as exc:
            raise DeviceConnectionError(f"Enable failed: {exc}", "enable") from exc

        errors: dict[str, str] = {}

        def run(command: str) -> str:
            self._info("Running command on %s: %s", getattr(connection, "base_prompt", "device"), command)
            output = _run_command(connection, command, errors)
            if command in errors:
                if self.logger:
                    self.logger.warning(
                        "[SSH] Command failed on %s: %s (%s)",
                        getattr(connection, "base_prompt", "device"),
                        command,
                        errors[command],
                    )
            else:
                self._info("Command completed: %s", command)
            return output

        return CommandOutputs(
            show_version=run("show version"),
            show_inventory=run("show inventory"),
            show_cdp_neighbors_detail=run("show cdp neighbors detail"),
            show_lldp_neighbors_detail=run("show lldp neighbors detail"),
            show_interfaces_trunk=run("show interfaces trunk"),
            show_interface_switchport=run("show interface switchport"),
            command_errors=errors,
        )


def _run_command(connection, command: str, errors: dict[str, str]) -> str:
    try:
        return connection.send_command(command, read_timeout=30, strip_prompt=True, strip_command=True)
    except Exception as exc:
        errors[command] = str(exc)
        return ""

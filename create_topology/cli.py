from __future__ import annotations

import argparse
from collections import Counter
import getpass
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from .discovery import DiscoveryEngine
from .outputs import write_html, write_interconnections_csv, write_inventory_csv, write_mermaid
from .ssh_client import CiscoSSHClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover Cisco devices over SSH using CDP/LLDP and render topology outputs."
    )
    parser.add_argument("--seed-ip", required=True, help="Seed IP address used to start discovery.")
    parser.add_argument("--username", help="SSH username. If omitted, the script will prompt for it.")
    parser.add_argument(
        "--output-dir",
        default=str(Path.cwd() / "output"),
        help="Base directory where a timestamped folder is created for each collection.",
    )
    parser.add_argument("--timeout", type=int, default=10, help="SSH connection timeout in seconds.")
    parser.add_argument(
        "--direct-neighbors",
        action="store_true",
        help="Connect directly to neighbors instead of using the seed as an SSH jump host.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum discovery depth (seed is 0). Use 0 for unlimited discovery.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    username = args.username or input("SSH username: ").strip()
    password = getpass.getpass("SSH password: ")
    secret = getpass.getpass("Enable password: ")
    if args.max_depth < 0:
        parser.error("--max-depth must be zero or greater")
    output_dir = _build_run_output_dir(Path(args.output_dir), args.seed_ip)

    logger = _configure_logging(output_dir)
    client = CiscoSSHClient(
        username=username,
        password=password,
        secret=secret,
        timeout=args.timeout,
        jump_host="" if args.direct_neighbors else args.seed_ip,
        logger=logger,
    )
    engine = DiscoveryEngine(client=client, logger=logger)

    logger.info("Starting discovery from seed %s", args.seed_ip)
    try:
        result = engine.discover(args.seed_ip, max_depth=args.max_depth)
    finally:
        client.close()

    status_counts = Counter(device.access_status for device in result.devices.values())
    logger.info(
        "[SUMMARY] success=%s | failed=%s | external=%s | no_ip=%s | depth_limit=%s",
        status_counts["success"],
        status_counts["failed"],
        status_counts["external"],
        status_counts["discovered_no_ip"],
        status_counts["depth_limit"],
    )
    logger.info("[OUTPUT] Writing inventory.csv")
    write_inventory_csv(output_dir / "inventory.csv", result.devices)
    logger.info("[OUTPUT] Writing interconnections.csv")
    write_interconnections_csv(output_dir / "interconnections.csv", result)
    logger.info("[OUTPUT] Writing topology.mmd")
    write_mermaid(output_dir / "topology.mmd", result)
    logger.info("[OUTPUT] Writing topology.html")
    write_html(output_dir / "topology.html", result)
    logger.info("[COMPLETE] Files written to %s", output_dir)

    print(f"Discovery completed. Files written to {output_dir}")
    return 0


def _build_run_output_dir(base_dir: Path, seed_ip: str, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    safe_seed = re.sub(r"[^A-Za-z0-9._-]+", "_", seed_ip).strip("._-") or "seed"
    candidate = base_dir / f"{safe_seed}-{timestamp}"
    if not candidate.exists():
        return candidate

    suffix = 2
    while True:
        alternate = base_dir / f"{safe_seed}-{timestamp}-{suffix:02d}"
        if not alternate.exists():
            return alternate
        suffix += 1


def _configure_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("create_topology")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(output_dir / "discovery.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)

    # Bibliotecas SSH podem emitir tracebacks internos mesmo quando a falha é
    # recuperada pelo programa. As exceções úteis são resumidas pelo logger da aplicação.
    for library_name in ("paramiko", "paramiko.transport", "netmiko"):
        library_logger = logging.getLogger(library_name)
        library_logger.handlers.clear()
        library_logger.addHandler(logging.NullHandler())
        library_logger.propagate = False
        library_logger.setLevel(logging.CRITICAL)
    return logger

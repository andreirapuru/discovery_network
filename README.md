# create_topology

Ferramenta em Python para descobrir e documentar uma rede a partir de um equipamento Cisco inicial (seed). O script acessa os equipamentos por SSH, coleta vizinhos CDP e LLDP, identifica interfaces trunk e gera inventário, tabela de interconexões e uma topologia HTML interativa.

## Como funciona

1. O usuário informa o IP do seed, o usuário SSH, a senha SSH e a senha de enable.
2. O script conecta ao seed e executa os comandos de inventário, CDP, LLDP e trunk.
3. Os vizinhos Cisco com IP de gerenciamento são adicionados à fila de descoberta.
4. Por padrão, os vizinhos são acessados por uma sessão SSH aberta a partir do seed.
5. O processo continua até atingir a profundidade configurada ou não existirem novos vizinhos.
6. Cada execução gera uma pasta própria com todos os resultados.

O script evita ciclos e acessos repetidos por IP. Informações complementares recebidas por CDP e LLDP na mesma porta são combinadas, inclusive quando um protocolo fornece o IP e o outro fornece hostname ou modelo.

## Requisitos

- Windows, Linux ou macOS com acesso SSH ao seed;
- Python 3.13 ou superior;
- seed Cisco IOS/IOS-XE com SSH e CDP/LLDP habilitados;
- usuário com permissão para executar os comandos de consulta;
- senha de enable quando necessária;
- no modo padrão, o seed deve permitir `ssh -l usuario IP` para alcançar os vizinhos;
- todos os equipamentos acessados devem aceitar o mesmo conjunto de credenciais nesta versão.

## Instalação

Abra o PowerShell ou terminal na pasta do projeto:

```powershell
python -m pip install -r requirements.txt
```

Se `python` não estiver disponível no Windows, tente:

```powershell
py -m pip install -r requirements.txt
```

Para corrigir ou atualizar uma instalação existente com as versões compatíveis:

```powershell
python -m pip install --upgrade --force-reinstall -r requirements.txt
```

O Paramiko está limitado à série 3.x porque versões posteriores removeram algoritmos ainda utilizados por alguns equipamentos Cisco antigos.

## Uso básico

```powershell
python main.py --seed-ip 10.10.10.1 --username admin
```

As senhas são solicitadas sem aparecer no terminal:

```text
SSH password:
Enable password:
```

Se `--username` não for informado, o programa também solicitará o usuário:

```powershell
python main.py --seed-ip 10.10.10.1
```

## Modos de conexão

### Vizinhos acessados pelo seed — padrão

Use quando o computador alcança apenas o seed. O script mantém uma sessão com ele e abre sequencialmente as sessões dos vizinhos pelo CLI Cisco:

```text
Computador → Seed → Vizinho
```

```powershell
python main.py --seed-ip 10.10.10.1 --username admin
```

### Acesso direto aos vizinhos

Use somente quando o computador puder abrir SSH diretamente para todos os equipamentos:

```powershell
python main.py --seed-ip 10.10.10.1 --username admin --direct-neighbors
```

## Profundidade da descoberta

O seed é o nível `0`. A profundidade padrão é `3`:

```powershell
python main.py --seed-ip 10.10.10.1 --username admin --max-depth 3
```

Para continuar até não existirem novos vizinhos Cisco com IP:

```powershell
python main.py --seed-ip 10.10.10.1 --username admin --max-depth 0
```

O valor não pode ser negativo. Uma descoberta ilimitada pode demorar em redes grandes ou com muitos equipamentos que não respondem ao SSH.

## Parâmetros

| Parâmetro | Obrigatório | Padrão | Descrição |
| --- | --- | --- | --- |
| `--seed-ip` | Sim | — | IP do equipamento inicial. |
| `--username` | Não | Prompt | Usuário SSH reutilizado nos equipamentos. |
| `--max-depth` | Não | `3` | Profundidade máxima; `0` significa ilimitada. |
| `--timeout` | Não | `10` | Timeout da conexão SSH, em segundos. |
| `--output-dir` | Não | `./output` | Pasta-base onde cada coleta será criada. |
| `--direct-neighbors` | Não | Desativado | Conecta diretamente aos vizinhos, sem usar o seed. |

Ajuda integrada:

```powershell
python main.py --help
```

## Acompanhamento da execução

O console mostra o equipamento atual, cada comando, os vizinhos encontrados e as próximas ações:

```text
[INFO] [DISCOVERY] Accessing IP 10.10.10.52 | depth=1 | discovered_by=CDP:ADDHQGW01
[INFO] [SSH] Opening neighbor 10.10.10.52 through seed 10.10.10.1
[INFO] [SSH] Running command on ADDHQSW02: show cdp neighbors detail
[INFO] [DEVICE] Collected ADDHQSW02 | ip=10.10.10.52 | model=WS-C2960S-48LPS-L | neighbors=4
[INFO] [NEIGHBOR] local=ADDHQSW02 Gi1/0/11 | remote=SEP001122334455 | ip=10.10.20.21 | port=Eth0 | protocol=CDP+LLDP
```

Falhas recuperáveis aparecem em uma única linha e não interrompem os demais equipamentos:

```text
[WARNING] [FAILED] ip=10.10.20.21 | category=authentication | reason=Password was requested again
```

Ao final:

```text
[INFO] [SUMMARY] success=6 | failed=4 | external=8 | no_ip=2 | depth_limit=0
[INFO] [COMPLETE] Files written to ...
```

Tracebacks internos do Paramiko e Netmiko são suprimidos. O log preserva o IP, a categoria e o motivo relevante da falha.

## Arquivos gerados

Cada execução cria uma subpasta usando o seed, a data e a hora:

```text
output/10.10.10.1-20261020-185349/
```

Se duas execuções começarem no mesmo segundo, será adicionado um sufixo como `-02`.

| Arquivo | Conteúdo |
| --- | --- |
| `inventory.csv` | Equipamentos, IPs, fabricante, modelo, versões, profundidade e estado. |
| `interconnections.csv` | Pontas dos enlaces, interfaces, protocolos e trunk em cada lado. |
| `topology.html` | Topologia interativa e autocontida para abrir no navegador. |
| `topology.mmd` | Versão textual do diagrama em Mermaid. |
| `discovery.log` | Histórico estruturado de todas as etapas da coleta. |

Para usar outra pasta-base:

```powershell
python main.py --seed-ip 10.10.10.1 --username admin --output-dir D:\Coletas
```

## Topologia HTML

Abra `topology.html` em um navegador. Não é necessário acesso à internet.

- Arraste equipamentos para reorganizar o desenho.
- Use a roda do mouse para zoom.
- Arraste o fundo para mover a visualização.
- Enlaces paralelos são exibidos como linhas separadas.
- Os enlaces mostram interfaces, CDP/LLDP e indicação de trunk.
- Clique em um equipamento ou enlace para ocultá-lo.
- Restaure itens pelo painel **Itens ocultos**.
- O painel informa hostname, IP, vizinho e interfaces do item oculto.

## Estados do inventário

| Estado | Significado |
| --- | --- |
| `success` | Equipamento acessado e comandos coletados. |
| `failed` | Equipamento descoberto, mas SSH, login, enable ou coleta falhou. |
| `external` | Vizinho que não será acessado como Cisco IOS, mas aparece na topologia. |
| `discovered_no_ip` | Vizinho Cisco identificado sem IP de gerenciamento. |
| `depth_limit` | Vizinho encontrado além da profundidade configurada. |

Quando o SSH falha, o script preserva hostname, modelo e fabricante aprendidos por CDP/LLDP sempre que disponíveis.

## Comandos executados

```text
show version
show inventory
show cdp neighbors detail
show lldp neighbors detail
show interfaces trunk
show interface switchport
```

Um comando não suportado é registrado, mas não interrompe os demais comandos nem a descoberta.

## Solução de problemas

### Senha solicitada novamente

O vizinho não aceitou o usuário ou a senha. Nesta versão não é possível informar credenciais diferentes por equipamento.

### Connection timed out

O seed não alcançou o SSH do vizinho ou não houve resposta no tempo configurado. Para aumentar o timeout:

```powershell
python main.py --seed-ip 10.10.10.1 --username admin --timeout 20
```

### Connection refused

O IP respondeu, mas a porta SSH está fechada ou recusou a conexão.

### Retorno ao prompt do seed

O SSH foi iniciado no seed, mas não abriu uma sessão válida no vizinho. A falha é registrada e o próximo equipamento é processado.

### No acceptable kex algorithm

Reinstale as dependências compatíveis:

```powershell
python -m pip install --upgrade --force-reinstall -r requirements.txt
```

Sempre que possível, atualize também a configuração SSH dos equipamentos para algoritmos modernos.

### Descoberta demorada

Equipamentos inacessíveis consomem o tempo de timeout. Reduza `--max-depth`, ajuste `--timeout` ou corrija a conectividade dos IPs indicados no log.

## Limitações atuais

- coleta ativa limitada a Cisco IOS/IOS-XE com o driver `cisco_ios`;
- um único conjunto de credenciais por execução;
- somente endereços informados por CDP/LLDP são acessados;
- não há varredura de sub-redes nem tentativa de adivinhar IPs ausentes;
- equipamentos não Cisco são documentados, mas não entram na fila de SSH;
- o resultado representa as informações disponíveis no momento da coleta.

## Segurança

- As senhas são lidas com prompt seguro.
- Credenciais não são gravadas nos arquivos de saída.
- O script executa somente comandos de consulta.
- A compatibilidade com SSH legado deve ser usada apenas quando necessária; prefira algoritmos modernos nos equipamentos.

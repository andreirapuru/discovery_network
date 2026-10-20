from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import DeviceRecord, DiscoveryResult
from .parsers import normalize_interface


def write_inventory_csv(path: Path, devices: dict[str, DeviceRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "hostname",
        "ip",
        "vendor",
        "model",
        "hardware_version",
        "software_version",
        "access_status",
        "access_error",
        "discovery_source",
        "discovery_depth",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in sorted(devices.values(), key=lambda item: (item.hostname or item.ip, item.ip)):
            writer.writerow({field: getattr(record, field) for field in fieldnames})


def write_mermaid(path: Path, result: DiscoveryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes: dict[str, tuple[str, str]] = {}
    edges: list[dict[str, object]] = []

    for device in result.devices.values():
        node_id = _node_id(device.ip or device.hostname or "unknown")
        label = f"{device.hostname or 'unknown'}\\n{device.ip or 'sem IP'}\\n{device.access_status}"
        nodes[node_id] = (label, device.vendor or "Unknown")

    for link in _consolidate_links(result):
        local_node = _node_id(str(link["a_ip"] or link["a_hostname"]))
        remote_node = _node_id(str(link["b_ip"] or link["b_hostname"] or "unknown"))

        if local_node not in nodes:
            nodes[local_node] = (f"{link['a_hostname'] or 'unknown'}\\n{link['a_ip'] or 'sem IP'}", "Cisco")
        if remote_node not in nodes:
            vendor = str(link["b_vendor"] or "Unknown")
            nodes[remote_node] = (
                f"{link['b_hostname'] or 'unknown'}\\n{link['b_ip'] or 'sem IP'}",
                vendor,
            )
        edges.append({"from": local_node, "to": remote_node, **link})

    lines = ["graph TD"]
    for node_id, (label, vendor) in sorted(nodes.items()):
        lines.append(f'    {node_id}["{_mermaid_text(label)}"]')
        if vendor.lower().startswith("cisco"):
            lines.append(f"    style {node_id} fill:#d9ecff,stroke:#1d4f91,stroke-width:2px")
        else:
            lines.append(f"    style {node_id} fill:#f5f5f5,stroke:#6b7280,stroke-width:1px")

    for edge in edges:
        trunk = " | trunk" if edge["a_is_trunk"] or edge["b_is_trunk"] else ""
        label = f"{edge['a_interface']} <-> {edge['b_interface']} | {edge['protocols']}{trunk}"
        lines.append(f'    {edge["from"]} ---|"{_mermaid_text(label)}"| {edge["to"]}')

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_interconnections_csv(path: Path, result: DiscoveryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "local_hostname",
        "local_ip",
        "local_interface",
        "remote_hostname",
        "remote_ip",
        "remote_interface",
        "protocol",
        "local_is_trunk",
        "remote_is_trunk",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for link in _consolidate_links(result):
            writer.writerow(
                {
                    "local_hostname": link["a_hostname"],
                    "local_ip": link["a_ip"],
                    "local_interface": link["a_interface"],
                    "remote_hostname": link["b_hostname"],
                    "remote_ip": link["b_ip"],
                    "remote_interface": link["b_interface"],
                    "protocol": link["protocols"],
                    "local_is_trunk": str(link["a_is_trunk"]).lower(),
                    "remote_is_trunk": str(link["b_is_trunk"]).lower(),
                }
            )


def write_html(path: Path, result: DiscoveryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes: dict[str, dict[str, object]] = {}
    for device in result.devices.values():
        key = device.ip or f"name:{device.hostname}"
        nodes[key] = {
            "id": key,
            "hostname": device.hostname or "desconhecido",
            "ip": device.ip or "sem IP",
            "model": device.model or "modelo desconhecido",
            "depth": device.discovery_depth,
            "status": device.access_status,
            "vendor": device.vendor or "Unknown",
        }
    links = _consolidate_links(result)
    for link in links:
        for side in ("a", "b"):
            key = str(link[f"{side}_ip"] or f"name:{link[f'{side}_hostname']}")
            link_hostname = str(link[f"{side}_hostname"] or "")
            existing = nodes.get(key)
            if existing:
                if existing["hostname"] in ("", "desconhecido") and link_hostname:
                    existing["hostname"] = link_hostname
                if existing["ip"] in ("", "sem IP") and link[f"{side}_ip"]:
                    existing["ip"] = link[f"{side}_ip"]
            else:
                nodes[key] = {
                    "id": key,
                    "hostname": link_hostname or "desconhecido",
                    "ip": link[f"{side}_ip"] or "sem IP",
                    "model": "modelo desconhecido",
                    "depth": None,
                    "status": "external",
                    "vendor": link.get(f"{side}_vendor", "Unknown"),
                }
        link["source"] = str(link["a_ip"] or f"name:{link['a_hostname']}")
        link["target"] = str(link["b_ip"] or f"name:{link['b_hostname']}")

    parallel_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for link in links:
        pair = tuple(sorted((str(link["source"]), str(link["target"]))))
        parallel_groups.setdefault(pair, []).append(link)
    for group in parallel_groups.values():
        for index, link in enumerate(group):
            link["parallel_index"] = index
            link["parallel_count"] = len(group)
    for index, link in enumerate(links):
        link["edge_id"] = f"edge-{index}"

    data = json.dumps({"nodes": list(nodes.values()), "links": links}, ensure_ascii=False).replace("</", "<\\/")
    document = _HTML_TEMPLATE.replace("__TOPOLOGY_DATA__", data)
    path.write_text(document, encoding="utf-8")


def _consolidate_links(result: DiscoveryResult) -> list[dict[str, object]]:
    links: dict[tuple[tuple[str, str], tuple[str, str]], dict[str, object]] = {}
    for neighbor in result.neighbors:
        local_id = neighbor.local_ip or f"name:{neighbor.local_hostname}"
        remote_id = neighbor.remote_ip or f"name:{neighbor.remote_hostname}"
        local_interface = normalize_interface(neighbor.local_interface)
        remote_interface = normalize_interface(neighbor.remote_interface)
        left = (local_id, local_interface)
        right = (remote_id, remote_interface)
        key = tuple(sorted((left, right)))
        a_snapshot = result.snapshots.get(neighbor.local_ip)
        observed_trunk = bool(
            a_snapshot and local_interface in {normalize_interface(item) for item in a_snapshot.trunks}
        )
        link = links.setdefault(key, {
            "a_hostname": neighbor.local_hostname,
            "a_ip": neighbor.local_ip,
            "a_interface": local_interface,
            "a_vendor": "Cisco",
            "b_hostname": neighbor.remote_hostname,
            "b_ip": neighbor.remote_ip,
            "b_interface": remote_interface,
            "b_vendor": neighbor.remote_vendor,
            "protocol_set": set(),
            "a_is_trunk": False,
            "b_is_trunk": False,
        })
        link["protocol_set"].update(neighbor.protocol.split("+"))
        observed_side = "a_is_trunk" if (local_id, local_interface) == (
            link["a_ip"] or f"name:{link['a_hostname']}", link["a_interface"]
        ) else "b_is_trunk"
        link[observed_side] = link[observed_side] or observed_trunk

    output: list[dict[str, object]] = []
    for key in sorted(links):
        link = links[key]
        link["protocols"] = "+".join(sorted(link.pop("protocol_set")))
        output.append(link)
    return output


def _node_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value)
    return f"node_{cleaned.strip('_') or 'unknown'}"


def _mermaid_text(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Topologia de rede</title><style>
html,body{margin:0;height:100%;font-family:Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0}header{padding:12px 18px;background:#111827;border-bottom:1px solid #334155}h1{font-size:18px;margin:0 0 4px}.hint{font-size:12px;color:#94a3b8}svg{width:100%;height:calc(100% - 64px);cursor:grab}svg:active{cursor:grabbing}.edge{stroke:#64748b;stroke-width:2;cursor:pointer}.edge.trunk{stroke:#f59e0b;stroke-width:3}.edge-label{fill:#cbd5e1;font-size:11px;text-anchor:middle;paint-order:stroke;stroke:#0f172a;stroke-width:4px;cursor:pointer}.node{cursor:pointer}.node rect{stroke-width:2;rx:9}.node text{fill:#f8fafc;text-anchor:middle;pointer-events:none}.success rect{fill:#075985;stroke:#38bdf8}.failed rect{fill:#7f1d1d;stroke:#f87171}.depth_limit rect{fill:#713f12;stroke:#fbbf24}.discovered_no_ip rect{fill:#4c1d95;stroke:#a78bfa}.external rect{fill:#334155;stroke:#94a3b8}#hidden-panel{position:fixed;right:14px;top:74px;width:260px;max-height:calc(100% - 100px);overflow:auto;background:#111827eF;border:1px solid #475569;border-radius:9px;padding:10px;z-index:10;box-shadow:0 8px 24px #0008}#hidden-panel:empty{display:none}#hidden-panel h2{font-size:13px;margin:0 0 8px;color:#cbd5e1}#hidden-panel button{display:block;width:100%;margin:5px 0;padding:7px 8px;text-align:left;border:1px solid #475569;border-radius:6px;background:#1e293b;color:#e2e8f0;cursor:pointer}#hidden-panel button:hover{background:#334155}
</style></head><body><header><h1>Topologia de rede</h1><div class="hint">Arraste para organizar. Clique em um equipamento ou enlace para ocultar; clique no painel para exibir novamente.</div></header><aside id="hidden-panel"></aside><svg id="canvas"><g id="viewport"></g></svg>
<script>const DATA=__TOPOLOGY_DATA__;const svg=document.getElementById('canvas'),view=document.getElementById('viewport'),hiddenPanel=document.getElementById('hidden-panel'),NS='http://www.w3.org/2000/svg';let scale=1,panX=0,panY=0,panning=false,dragging=null,start={x:0,y:0};const hiddenNodes=new Set(),hiddenEdges=new Set();
const pos=new Map(DATA.nodes.map((n,i)=>[n.id,{x:160+(i%5)*260,y:100+Math.floor(i/5)*180}]));function el(n,a={}){const x=document.createElementNS(NS,n);Object.entries(a).forEach(([k,v])=>x.setAttribute(k,v));return x}function render(){view.innerHTML='';DATA.links.forEach(l=>{if(hiddenEdges.has(l.edge_id)||hiddenNodes.has(l.source)||hiddenNodes.has(l.target))return;const a=pos.get(l.source),b=pos.get(l.target);if(!a||!b)return;const dx=b.x-a.x,dy=b.y-a.y,len=Math.max(1,Math.hypot(dx,dy)),offset=(l.parallel_index-(l.parallel_count-1)/2)*42,nx=-dy/len,ny=dx/len,cx=(a.x+b.x)/2+nx*offset,cy=(a.y+b.y)/2+ny*offset;const path=el('path',{d:`M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`,fill:'none',class:'edge '+((l.a_is_trunk||l.b_is_trunk)?'trunk':'')});const hide=()=>{hiddenEdges.add(l.edge_id);render()};path.addEventListener('click',hide);view.append(path);const t=el('text',{x:(a.x+2*cx+b.x)/4,y:(a.y+2*cy+b.y)/4-7,class:'edge-label'});t.textContent=edgeName(l);t.addEventListener('click',hide);view.append(t)});DATA.nodes.forEach(n=>{if(hiddenNodes.has(n.id))return;const p=pos.get(n.id),g=el('g',{class:'node '+n.status,transform:`translate(${p.x-95} ${p.y-42})`});g.dataset.id=n.id;g.append(el('rect',{width:190,height:84}));[['hostname',n.hostname,19],['ip',n.ip,39],['info',n.model,58],['status',n.status,75]].forEach(([,v,y],i)=>{const t=el('text',{x:95,y,'font-size':i?11:14,'font-weight':i?'400':'600'});t.textContent=v;g.append(t)});view.append(g);dragNode(g)});transform();updateHiddenPanel()}function edgeName(l){return `${l.a_hostname||l.a_ip}:${l.a_interface} ↔ ${l.b_hostname||l.b_ip}:${l.b_interface} | ${l.protocols}` }function nodeName(n){const connected=DATA.links.filter(l=>l.source===n.id||l.target===n.id),link=connected[0];let context='sem conexão identificada';if(link){const isA=link.source===n.id,neighbor=isA?(link.b_hostname||link.b_ip):(link.a_hostname||link.a_ip),localPort=isA?link.a_interface:link.b_interface,remotePort=isA?link.b_interface:link.a_interface;context=`vizinho ${neighbor} (${localPort} ↔ ${remotePort})`;if(connected.length>1)context+=` +${connected.length-1} conexão(ões)`}return `${n.hostname||'desconhecido'} | ${n.ip||'sem IP'} | ${context}`}function updateHiddenPanel(){hiddenPanel.innerHTML='';if(!hiddenNodes.size&&!hiddenEdges.size)return;const h=document.createElement('h2');h.textContent='Itens ocultos — clique para exibir';hiddenPanel.append(h);hiddenNodes.forEach(id=>{const n=DATA.nodes.find(x=>x.id===id),b=document.createElement('button');b.textContent=`Equipamento: ${n?nodeName(n):id}`;b.onclick=()=>{hiddenNodes.delete(id);render()};hiddenPanel.append(b)});hiddenEdges.forEach(id=>{const l=DATA.links.find(x=>x.edge_id===id),b=document.createElement('button');b.textContent=`Enlace: ${l?edgeName(l):id}`;b.onclick=()=>{hiddenEdges.delete(id);render()};hiddenPanel.append(b)})}function transform(){view.setAttribute('transform',`translate(${panX} ${panY}) scale(${scale})`)}function point(e){const r=svg.getBoundingClientRect();return{x:(e.clientX-r.left-panX)/scale,y:(e.clientY-r.top-panY)/scale}}function dragNode(g){g.addEventListener('pointerdown',e=>{e.stopPropagation();const p=point(e),o=pos.get(g.dataset.id);dragging={id:g.dataset.id,dx:p.x-o.x,dy:p.y-o.y,startX:e.clientX,startY:e.clientY,moved:false};svg.setPointerCapture(e.pointerId)})}svg.addEventListener('wheel',e=>{e.preventDefault();scale=Math.max(.25,Math.min(3,scale*(e.deltaY<0?1.1:.9)));transform()},{passive:false});svg.addEventListener('pointerdown',e=>{if(e.target.closest('.node'))return;panning=true;start={x:e.clientX-panX,y:e.clientY-panY};svg.setPointerCapture(e.pointerId)});svg.addEventListener('pointermove',e=>{if(dragging){if(Math.hypot(e.clientX-dragging.startX,e.clientY-dragging.startY)>5)dragging.moved=true;const q=point(e),o=pos.get(dragging.id);o.x=q.x-dragging.dx;o.y=q.y-dragging.dy;render()}else if(panning){panX=e.clientX-start.x;panY=e.clientY-start.y;transform()}});svg.addEventListener('pointerup',()=>{if(dragging&&!dragging.moved)hiddenNodes.add(dragging.id);panning=false;dragging=null;render()});render();</script></body></html>'''

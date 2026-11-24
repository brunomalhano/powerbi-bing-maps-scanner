#!/usr/bin/env python3
"""Inventario read-only de visuais Bing Maps (Map e Filled Map) no Power BI/Fabric.

API de inventario: Power BI Admin WorkspaceInfo.
API de definicao: Fabric Get Report Definition (PBIR / PBIR-Legacy).
API de uso: Power BI Admin Activity Events (ViewReport, ultimos 28 dias).
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

POWERBI = "https://api.powerbi.com/v1.0/myorg"
FABRIC = "https://api.fabric.microsoft.com/v1"
LEGACY = {"map": "Bing Maps (Map)", "filledMap": "Bing Maps (Filled Map)"}
MAP_TYPES = {**LEGACY, "azureMap": "Azure Maps"}
LOG = logging.getLogger("pbi-maps")


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, tenant: str, app_id: str, secret: str):
        import msal  # dependencia necessaria somente ao conectar a API
        self.auth = msal.ConfidentialClientApplication(
            app_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential=secret,
        )
        self.session = requests.Session()
        self.token_cache = {}

    def token(self, scope: str) -> str:
        if scope not in self.token_cache:
            response = self.auth.acquire_token_for_client(scopes=[scope])
            if "access_token" not in response:
                raise ApiError(f"Falha de autenticacao: {response.get('error_description') or response}")
            self.token_cache[scope] = response["access_token"]
        return self.token_cache[scope]

    def request(self, method: str, url: str, *, params=None, data=None, timeout=90):
        domain = urlparse(url).hostname
        if domain not in {"api.powerbi.com", "api.fabric.microsoft.com"}:
            raise ApiError(f"Dominio de API inesperado: {domain}")
        scope = ("https://api.fabric.microsoft.com/.default" if domain == "api.fabric.microsoft.com"
                 else "https://analysis.windows.net/powerbi/api/.default")
        for attempt in range(7):
            headers = {"Authorization": f"Bearer {self.token(scope)}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.session.request(
                    method, url, params=params, json=data, headers=headers, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt == 6:
                    raise ApiError(f"Erro de rede: {exc}") from exc
                time.sleep(min(2 ** attempt, 45))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 6:
                raw = response.headers.get("Retry-After", "")
                delay = float(raw) if raw.isdigit() else min(2 ** attempt + 1, 60)
                LOG.warning("HTTP %d; repetindo em %.0f s: %s", response.status_code, delay, url)
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code} {url}: {response.text[:350]}")
            return response
        raise ApiError(f"Limite de tentativas para {url}")

    def get_json(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, *, data=None, params=None):
        return self.request("POST", url, data=data, params=params)


def admin_inventory(client: Client, include_personal=False):
    """Inventario tenant-wide; requer admin API autorizada para o principal."""
    modified = client.get_json(
        f"{POWERBI}/admin/workspaces/modified",
        params={"excludePersonalWorkspaces": str(not include_personal).lower()},
    )
    ids = [item["id"] for item in modified if item.get("id")]
    LOG.info("Workspaces descobertos: %d", len(ids))
    reports = []
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        request = client.post_json(
            f"{POWERBI}/admin/workspaces/getInfo",
            data={"workspaces": batch},
            params={"getArtifactUsers": "false", "lineage": "false"},
        ).json()
        scan_id = request["id"]
        for _ in range(40):  # ate ~20 minutos por lote
            state = client.get_json(f"{POWERBI}/admin/workspaces/scanStatus/{scan_id}")
            status = state.get("status", "")
            if status == "Succeeded":
                break
            if status in ("Failed", "Error"):
                raise ApiError(f"Scan {scan_id} terminou com {status}: {state}")
            time.sleep(30)
        else:
            raise ApiError(f"Timeout no scan administrativo {scan_id}")
        result = client.get_json(f"{POWERBI}/admin/workspaces/scanResult/{scan_id}")
        for workspace in result.get("workspaces", []):
            workspace_id = workspace.get("id", "")
            for report in workspace.get("reports", []) or []:
                if report.get("reportType") == "PaginatedReport" or report.get("format") == "RDL":
                    continue
                rid = report.get("id")
                if not workspace_id or not rid:
                    continue
                reports.append({
                    "workspace_id": workspace_id,
                    "workspace": workspace.get("name", ""),
                    "report_id": rid,
                    "report": report.get("name", ""),
                    "format_hint": report.get("format", ""),
                    "owner": report.get("createdBy", ""),
                    "last_modified_by": report.get("modifiedBy", ""),
                    "url": f"https://app.powerbi.com/groups/{workspace_id}/reports/{rid}",
                })
        LOG.info("Inventario: %d/%d workspaces; %d relatorios", min(offset + 100, len(ids)), len(ids), len(reports))
    # deduplica por workspace e report ID
    return list({(r["workspace_id"], r["report_id"]): r for r in reports}.values())


def get_definition(client: Client, workspace_id: str, report_id: str):
    """Le definicao, tratando retorno sincrono 200 ou operacao Fabric 202."""
    url = f"{FABRIC}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    response = client.post_json(url)
    if response.status_code == 200:
        return response.json()["definition"]["parts"]
    if response.status_code != 202:
        raise ApiError(f"Resposta inesperada de getDefinition: {response.status_code}")
    operation = response.headers.get("x-ms-operation-id")
    if not operation:
        raise ApiError("202 sem x-ms-operation-id")
    for _ in range(90):
        delay = response.headers.get("Retry-After", "")
        time.sleep(min(int(delay) if delay.isdigit() else 5, 60))
        response = client.request("GET", f"{FABRIC}/operations/{operation}")
        state = response.json()
        status = state.get("status", "")
        if status == "Succeeded":
            return client.get_json(f"{FABRIC}/operations/{operation}/result")["definition"]["parts"]
        if status in ("Failed", "Cancelled", "Canceled"):
            raise ApiError(f"getDefinition {operation}: {state}")
    raise ApiError("Timeout na operacao Fabric getDefinition")


def decode_json_payload(payload: str):
    return json.loads(base64.b64decode(payload).decode("utf-8-sig"))


def parse_visuals(parts):
    """Extrai apenas mapas, evitando falsos positivos por busca textual bruta."""
    data = {}
    for item in parts:
        path = item.get("path", "").replace("\\", "/")
        if not path.endswith(".json"):
            continue
        if (path == "report.json" or re.search(r"^definition/pages/[^/]+/(?:page|visuals/[^/]+/visual)\.json$", path)):
            data[path] = decode_json_payload(item["payload"])

    rows = []
    # PBIR: um arquivo visual.json por visual
    paths = [p for p in data if re.fullmatch(r"definition/pages/[^/]+/visuals/[^/]+/visual\.json", p)]
    if paths:
        for path in paths:
            segments = path.split("/")
            page_id, visual_id = segments[2], segments[4]
            page = data.get(f"definition/pages/{page_id}/page.json", {})
            content = data[path]
            v = content.get("visual", {})
            visual_type = v.get("visualType") or content.get("visualType")
            if visual_type in MAP_TYPES:
                rows.append({
                    "page": page.get("displayName") or page.get("name") or page_id,
                    "page_id": page_id,
                    "visual_id": content.get("name") or visual_id,
                    "visual_type": visual_type,
                    "visual_name": MAP_TYPES[visual_type],
                    "migration_needed": visual_type in LEGACY,
                })
        return rows, "PBIR"

    # PBIR sem visuais: pode conter apenas paginas vazias
    if any(p.startswith("definition/pages/") for p in data):
        return [], "PBIR"

    # PBIR-Legacy: um report.json com sections[*].visualContainers[*].config (JSON string)
    legacy = data.get("report.json")
    if legacy is None:
        raise ValueError("Sem arquivos de paginas PBIR nem report.json PBIR-Legacy")
    if not isinstance(legacy.get("sections"), list):
        raise ValueError("report.json legado sem 'sections' (formato nao reconhecido)")
    for page in legacy["sections"]:
        for index, container in enumerate(page.get("visualContainers", []) or []):
            config = container.get("config", {})
            if isinstance(config, str):
                config = json.loads(config) if config.strip() else {}
            single = config.get("singleVisual") or {}
            visual_type = single.get("visualType") or config.get("visualType")
            if visual_type in MAP_TYPES:
                rows.append({
                    "page": page.get("displayName") or page.get("name") or "",
                    "page_id": page.get("name", ""),
                    "visual_id": config.get("name") or str(index),
                    "visual_type": visual_type,
                    "visual_name": MAP_TYPES[visual_type],
                    "migration_needed": visual_type in LEGACY,
                })
    return rows, "PBIR-Legacy"


def activity_events(client: Client, days: int):
    """Consulta uma janela por dia UTC; respeita continuacao da API."""
    today = dt.datetime.now(dt.timezone.utc).date()
    out = defaultdict(list)
    unmatched = 0
    for days_ago in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=days_ago)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        end = dt.datetime.combine(day, dt.time.max, tzinfo=dt.timezone.utc)
        if day == today:
            end = dt.datetime.now(dt.timezone.utc)
        params = {
            "startDateTime": f"'{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}'",
            "endDateTime": f"'{end.strftime('%Y-%m-%dT%H:%M:%S.%f')[:23]}Z'",
            "$filter": "Activity eq 'ViewReport'",
        }
        url = f"{POWERBI}/admin/activityevents"
        pages = 0
        while url:
            payload = client.get_json(url, params=params)
            params = None  # continuationUri ja inclui os parametros necessarios
            pages += 1
            for event in payload.get("activityEventEntities", []) or []:
                if (event.get("Activity") or event.get("Operation")) != "ViewReport":
                    continue
                report_id = event.get("ReportId") or event.get("reportId")
                if not report_id:
                    unmatched += 1
                    continue
                out[str(report_id).lower()].append(event)
            url = payload.get("continuationUri")
        LOG.info("Auditoria %s: %d paginas", day, pages)
    return out, unmatched


def write_csv(path: Path, records: list, fields: list):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def main():
    parser = argparse.ArgumentParser(description="Scanner tenant-wide de Bing Maps para migracao Azure Maps")
    parser.add_argument("--days", type=int, default=28, help="Dias para eventos ViewReport (1-28)")
    parser.add_argument("--skip-activity", action="store_true", help="Ignorar logs (inventario apenas)")
    parser.add_argument("--include-personal", action="store_true", help="Incluir My workspaces na descoberta")
    parser.add_argument("--out", default="output", help="Diretorio de saida")
    args = parser.parse_args()
    if not 1 <= args.days <= 28:
        parser.error("--days deve estar entre 1 e 28")
    for name in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"):
        if not os.getenv(name):
            parser.error(f"Variavel {name} nao definida")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = Client(os.environ["TENANT_ID"], os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"])
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    try:
        reports = admin_inventory(client, args.include_personal)
    except Exception as exc:
        LOG.error("Falha no inventario. Verifique habilitacao de Admin APIs: %s", exc)
        return 2

    visual_rows = []
    for i, report in enumerate(reports, start=1):
        try:
            parts = get_definition(client, report["workspace_id"], report["report_id"])
            vis, fmt = parse_visuals(parts)
            report["inspection_status"] = "OK"
            report["detected_format"] = fmt
            report["inspection_error"] = ""
            for item in vis:
                visual_rows.append({**report, **item})
        except Exception as exc:
            report["inspection_status"] = "ERROR"
            report["detected_format"] = ""
            report["inspection_error"] = str(exc)[:450]
            LOG.warning("Nao inspecionado: %s/%s: %s", report["workspace"], report["report"], exc)
        if i % 25 == 0:
            LOG.info("Definicoes processadas: %d/%d", i, len(reports))

    audit_status = "SKIPPED" if args.skip_activity else "OK"
    views, audit_missing_ids = {}, 0
    if not args.skip_activity:
        try:
            views, audit_missing_ids = activity_events(client, args.days)
        except Exception as exc:
            audit_status = "ERROR"
            LOG.warning("Falha nos logs de atividade: %s. Contagens nao serao inferidas como zero.", exc)

    visuals_by_report = defaultdict(list)
    for visual in visual_rows:
        visuals_by_report[visual["report_id"].lower()].append(visual)

    for report in reports:
        rid = report["report_id"].lower()
        vrows = visuals_by_report[rid]
        legacy = [v for v in vrows if v["migration_needed"]]
        report["bing_visuals"] = len(legacy)
        report["azure_visuals"] = sum(v["visual_type"] == "azureMap" for v in vrows)
        report["migration_status"] = (
            "NAO_INSPECIONADO" if report["inspection_status"] != "OK" else
            "MIGRAR" if legacy else
            "JA_USA_AZURE_MAPS" if vrows else "SEM_MAPAS_IDENTIFICADOS"
        )
        events = views.get(rid, []) if audit_status == "OK" else []
        users = {e.get("UserId") for e in events if e.get("UserId")}
        report["views_period"] = len(events) if audit_status == "OK" else ""
        report["unique_viewers_period"] = len(users) if audit_status == "OK" else ""
        report["audit_status"] = audit_status

    users_rows = []
    if audit_status == "OK":
        for report in reports:
            if report["migration_status"] != "MIGRAR":
                continue
            by_user = defaultdict(list)
            for event in views.get(report["report_id"].lower(), []):
                if event.get("UserId"):
                    by_user[event["UserId"]].append(event)
            for user, events in sorted(by_user.items()):
                users_rows.append({
                    **report,
                    "user_id": user,
                    "views": len(events),
                    "last_view_utc": max(str(e.get("CreationTime", "")) for e in events),
                })

    fields_report = [
        "workspace", "workspace_id", "report", "report_id", "url", "owner", "last_modified_by",
        "format_hint", "detected_format", "inspection_status", "inspection_error", "migration_status",
        "bing_visuals", "azure_visuals", "views_period", "unique_viewers_period", "audit_status",
    ]
    write_csv(output / "reports.csv", reports, fields_report)
    write_csv(output / "visuals.csv", visual_rows, fields_report + [
        "page", "page_id", "visual_id", "visual_type", "visual_name", "migration_needed"
    ])
    write_csv(output / "users.csv", users_rows, fields_report + ["user_id", "views", "last_view_utc"])

    summary = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workspaces_in_reports": len({r["workspace_id"] for r in reports}),
        "reports_in_inventory": len(reports),
        "reports_inspected": sum(r["inspection_status"] == "OK" for r in reports),
        "reports_not_inspected": sum(r["inspection_status"] != "OK" for r in reports),
        "reports_to_migrate": sum(r["migration_status"] == "MIGRAR" for r in reports),
        "bing_visuals": sum(v["migration_needed"] for v in visual_rows),
        "azure_maps_visuals": sum(v["visual_type"] == "azureMap" for v in visual_rows),
        "auditing_status": audit_status,
        "audit_days": args.days if audit_status == "OK" else None,
        "events_missing_report_id": audit_missing_ids if audit_status == "OK" else None,
        "unique_viewers_on_reports_to_migrate": (
            len({x["user_id"] for x in users_rows}) if audit_status == "OK" else None
        ),
        "note": "ViewReport mede acesso ao relatorio, nao interacao com o visual. Erros impedem alegar cobertura total.",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG.info("Resumo: %s", json.dumps(summary, ensure_ascii=False))
    if summary["reports_not_inspected"] or audit_status == "ERROR":
        return 1  # resultados parciais: nao tratar como inventario completo
    return 0


if __name__ == "__main__":
    sys.exit(main())

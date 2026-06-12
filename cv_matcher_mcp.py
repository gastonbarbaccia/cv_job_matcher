#!/usr/bin/env python3
"""
CV Job Matcher — MCP Server
Herramienta MCP para buscar empleos en LinkedIn que se ajusten a tu CV.

Registrar en Claude Code:
    claude mcp add cv-job-matcher python3 /home/gastonbarbaccia/cv_job_matcher/cv_matcher_mcp.py \
      -e ANTHROPIC_API_KEY=sk-ant-... \
      -e LINKEDIN_EMAIL=tu@email.com \
      -e LINKEDIN_PASSWORD=tu_pass
"""

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime

import pdfplumber
import anthropic
from linkedin_api import Linkedin
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()


# ─── Config ──────────────────────────────────────────────────────────────────

MODEL           = "claude-sonnet-4-6"
RATE_LIMIT_SECS = 0.4
MAX_SCORE_JOBS  = 40

mcp = FastMCP(
    "cv-job-matcher",
    instructions=(
        "Herramienta para buscar empleos en LinkedIn que se ajusten a un CV. "
        "Usa `leer_cv` primero para analizar el CV, luego `buscar_empleos` para obtener "
        "ofertas rankeadas. Podes usar `pipeline_completo` para hacer todo en un solo paso."
    ),
)

# Cache en memoria para no releer el CV en cada llamada
_cv_cache: dict = {}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_clients():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    li_email = os.environ.get("LINKEDIN_EMAIL", "")
    li_pass  = os.environ.get("LINKEDIN_PASSWORD", "")
    if not api_key:
        raise ValueError("Falta ANTHROPIC_API_KEY en las variables de entorno del MCP.")
    if not li_email or not li_pass:
        raise ValueError("Faltan LINKEDIN_EMAIL o LINKEDIN_PASSWORD en las variables de entorno del MCP.")
    return anthropic.Anthropic(api_key=api_key), li_email, li_pass


def _extract_pdf_text(pdf_path: str) -> str:
    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"No se encontró el PDF: {pdf_path}")
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    text = "\n".join(parts)
    if not text.strip():
        raise ValueError("El PDF no contiene texto extraíble.")
    return text


def _parse_json_response(raw: str) -> dict:
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"Respuesta no era JSON válido:\n{raw[:300]}")


def _analyze_cv_with_claude(client: anthropic.Anthropic, cv_text: str) -> dict:
    prompt = f"""Analiza el siguiente CV y extraé información estructurada.
Respondé SOLO con JSON válido, sin texto adicional ni bloques de código.

CV:
{cv_text[:12000]}

JSON a retornar:
{{
  "nombre": "nombre completo",
  "titulo_actual": "rol o título principal",
  "años_experiencia": 0,
  "resumen": "2-3 oraciones del perfil profesional",
  "skills_tecnicos": ["lista de tecnologías y herramientas"],
  "skills_blandos": ["liderazgo", "comunicación"],
  "idiomas": ["Español - nativo", "Inglés - avanzado"],
  "educacion": "título más alto",
  "sectores": ["industrias en las que trabajó"],
  "roles_objetivo": ["3-5 títulos de trabajo que encajan con el perfil"],
  "keywords_busqueda": ["6-8 términos para buscar en LinkedIn"],
  "ubicacion": "ciudad o región si está en el CV"
}}"""
    resp = client.messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return _parse_json_response(resp.content[0].text)


def _search_jobs(li: Linkedin, queries: list[dict], limit: int) -> list[dict]:
    seen, jobs = set(), []
    per_q = max(10, limit // max(len(queries), 1))

    for q in queries:
        kw  = q.get("keywords", "")
        loc = q.get("location", "")
        try:
            results = li.search_jobs(
                keywords=kw,
                location_name=loc or None,
                limit=per_q,
            )
        except Exception as e:
            continue

        for job in results:
            jid = job.get("entityUrn", "") or job.get("trackingUrn", "")
            if jid in seen:
                continue
            seen.add(jid)
            detail_id = jid.split(":")[-1]
            try:
                details = li.get_job(detail_id) if detail_id.isdigit() else {}
            except Exception:
                details = {}

            desc = details.get("description", {})
            if isinstance(desc, dict):
                desc = desc.get("text", "")
            desc = str(desc or "")

            jobs.append({
                "id": detail_id,
                "title": str(job.get("title") or details.get("title") or "Sin título"),
                "company": str(
                    job.get("companyName") or
                    details.get("companyDetails", {}).get("companyResolutionResult", {}).get("name", "") or
                    "Empresa desconocida"
                ),
                "location": str(details.get("formattedLocation") or job.get("formattedLocation") or ""),
                "workplace": str((details.get("workplaceTypes") or [""])[0]),
                "description": desc[:3000],
                "url": f"https://www.linkedin.com/jobs/view/{detail_id}" if detail_id.isdigit() else "",
                "query": kw,
            })
            time.sleep(RATE_LIMIT_SECS)
            if len(jobs) >= limit:
                break
        if len(jobs) >= limit:
            break

    return jobs[:limit]


def _score_job(client: anthropic.Anthropic, cv_info: dict, job: dict) -> dict:
    cv_summary = json.dumps({
        "titulo": cv_info.get("titulo_actual"),
        "años_exp": cv_info.get("años_experiencia"),
        "skills": cv_info.get("skills_tecnicos", [])[:20],
        "idiomas": cv_info.get("idiomas", []),
        "sectores": cv_info.get("sectores", []),
    }, ensure_ascii=False)

    prompt = f"""Evalúa qué tan bien se ajusta esta oferta al candidato.

CANDIDATO: {cv_summary}

OFERTA:
Título: {job['title']}
Empresa: {job['company']}
Ubicación: {job['location']}
Modalidad: {job['workplace']}
Descripción: {job['description'][:2000]}

Respondé SOLO con JSON:
{{
  "score": 85,
  "match_nivel": "Excelente|Muy bueno|Bueno|Regular|Bajo",
  "skills_coincidentes": ["skill1", "skill2"],
  "skills_faltantes": ["skill3"],
  "puntos_fuertes": "por qué es buen match",
  "puntos_debiles": "gaps o consideraciones",
  "recomendacion": "recomendación personalizada en una oración"
}}"""

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_json_response(resp.content[0].text)
    except Exception:
        return {"score": 0, "match_nivel": "Error", "skills_coincidentes": [],
                "skills_faltantes": [], "puntos_fuertes": "", "puntos_debiles": "",
                "recomendacion": ""}


def _generate_html(cv_info: dict, scored_jobs: list[dict], output_path: str, min_score: int):
    fecha   = datetime.now().strftime("%d/%m/%Y %H:%M")
    nombre  = cv_info.get("nombre", "Candidato")
    titulo  = cv_info.get("titulo_actual", "")
    ranked  = sorted(scored_jobs, key=lambda x: x.get("score_data", {}).get("score", 0), reverse=True)
    top     = [j for j in ranked if j.get("score_data", {}).get("score", 0) >= min_score]

    def sc(s):
        return "#30d158" if s>=80 else "#ffd60a" if s>=65 else "#ff9500" if s>=50 else "#ff453a"
    def sb(s):
        return "#00200a" if s>=80 else "#2d2700" if s>=65 else "#2d1a00" if s>=50 else "#3d0000"
    def nivel(n):
        colors = {"Excelente":("#30d158","#00200a"),"Muy bueno":("#7ac96a","#0a2000"),
                  "Bueno":("#ffd60a","#2d2700"),"Regular":("#ff9500","#2d1a00"),"Bajo":("#ff453a","#3d0000")}
        c,bg = colors.get(n,("#8b949e","#161b22"))
        return f'<span style="background:{bg};color:{c};border:1px solid {c};border-radius:12px;padding:2px 10px;font-size:11px;font-weight:600">{n}</span>'

    cards = ""
    for rank, job in enumerate(top, 1):
        sd    = job.get("score_data", {})
        score = sd.get("score", 0)
        ok_skills = "".join(
            f'<span style="background:#002800;color:#3fb950;border:1px solid #3fb950;border-radius:4px;padding:1px 8px;font-size:11px;margin:2px;display:inline-block">{s}</span>'
            for s in sd.get("skills_coincidentes", [])[:8])
        miss_skills = "".join(
            f'<span style="background:#3d0000;color:#ff7b72;border:1px solid #ff7b72;border-radius:4px;padding:1px 8px;font-size:11px;margin:2px;display:inline-block">{s}</span>'
            for s in sd.get("skills_faltantes", [])[:5])
        url_btn = f'<a href="{job["url"]}" target="_blank" style="display:inline-block;background:#1a3a5c;color:#58a6ff;border:1px solid #58a6ff;border-radius:6px;padding:6px 16px;font-size:13px;font-weight:600;text-decoration:none;margin-top:12px">Ver en LinkedIn →</a>' if job.get("url") else ""
        cards += f"""
<div style="background:#161b22;border:1px solid #30363d;border-radius:12px;margin-bottom:20px;overflow:hidden">
  <div style="display:flex;align-items:flex-start;gap:20px;padding:20px 24px;border-bottom:1px solid #30363d">
    <div style="min-width:72px;text-align:center">
      <div style="font-size:11px;color:#8b949e;margin-bottom:2px">#{rank}</div>
      <div style="font-size:40px;font-weight:800;font-family:monospace;color:{sc(score)};line-height:1;background:{sb(score)};border:2px solid {sc(score)};border-radius:10px;padding:6px 8px">{score}</div>
      <div style="font-size:10px;color:{sc(score)};margin-top:4px;font-weight:600">/100</div>
    </div>
    <div style="flex:1">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <h3 style="font-size:16px;font-weight:700;color:#fff;margin:0">{job['title']}</h3>
        {nivel(sd.get('match_nivel',''))}
      </div>
      <div style="font-size:14px;color:#58a6ff;margin-bottom:4px">🏢 {job['company']}</div>
      <div style="font-size:13px;color:#8b949e">📍 {job.get('location','N/A')} &nbsp;|&nbsp; {job.get('workplace','')}</div>
    </div>
  </div>
  <div style="padding:18px 24px">
    {'<div style="margin-bottom:12px"><div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8b949e;margin-bottom:6px;font-weight:600">Skills coincidentes</div>' + ok_skills + '</div>' if ok_skills else ''}
    {'<div style="margin-bottom:12px"><div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8b949e;margin-bottom:6px;font-weight:600">Skills a desarrollar</div>' + miss_skills + '</div>' if miss_skills else ''}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px">
      <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
        <div style="font-size:11px;color:#3fb950;font-weight:600;margin-bottom:4px">✓ Puntos fuertes</div>
        <div style="font-size:13px;color:#c9d1d9">{sd.get('puntos_fuertes','')}</div>
      </div>
      <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
        <div style="font-size:11px;color:#ff9500;font-weight:600;margin-bottom:4px">△ A considerar</div>
        <div style="font-size:13px;color:#c9d1d9">{sd.get('puntos_debiles','') or '—'}</div>
      </div>
    </div>
    {'<div style="background:#1a1a2e;border-left:3px solid #58a6ff;border-radius:0 6px 6px 0;padding:10px 14px;font-size:13px;color:#a5d6ff;margin-bottom:4px"><strong>💡</strong> ' + sd.get("recomendacion","") + '</div>' if sd.get("recomendacion") else ''}
    {url_btn}
  </div>
</div>"""

    skills_html = "".join(
        f'<span style="background:#21262d;border:1px solid #30363d;border-radius:4px;padding:2px 8px;font-size:12px;margin:2px;display:inline-block;color:#c9d1d9">{s}</span>'
        for s in cv_info.get("skills_tecnicos", [])[:15])

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CV Match — {nombre}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;font-size:14px}}.page{{max-width:1000px;margin:0 auto;padding:32px 20px}}</style>
</head><body><div class="page">
  <div style="background:linear-gradient(135deg,#0d1117,#161b22,#0a1628);border:1px solid #30363d;border-radius:12px;padding:32px;margin-bottom:28px">
    <div style="font-size:13px;color:#8b949e;margin-bottom:8px">Reporte de Matching CV ↔ LinkedIn — {fecha}</div>
    <h1 style="font-size:26px;font-weight:700;color:#fff;margin-bottom:4px">{nombre}</h1>
    <div style="font-size:16px;color:#58a6ff;margin-bottom:16px">{titulo}</div>
    <div style="margin-bottom:12px">{skills_html}</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:20px">
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:28px;font-weight:800;color:#fff">{len(scored_jobs)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px">Analizadas</div>
      </div>
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:28px;font-weight:800;color:#30d158">{len(top)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px">Con score ≥{min_score}</div>
      </div>
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:28px;font-weight:800;color:#ffd60a">{max((j.get('score_data',{}).get('score',0) for j in scored_jobs),default=0)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px">Score máximo</div>
      </div>
    </div>
  </div>
  <div style="font-size:18px;font-weight:700;color:#fff;border-bottom:1px solid #30363d;padding-bottom:12px;margin-bottom:20px">
    🏆 Mejores matches para tu perfil
  </div>
  {cards or '<div style="color:#8b949e;padding:20px;text-align:center">No se encontraron ofertas con score suficiente. Probá con min_score más bajo o distintas keywords.</div>'}
  <div style="border-top:1px solid #30363d;padding-top:16px;color:#8b949e;font-size:12px;text-align:center;margin-top:32px">
    CV Job Matcher MCP · {fecha} · {MODEL}
  </div>
</div></body></html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


# ─── Tools MCP ───────────────────────────────────────────────────────────────

@mcp.tool()
def leer_cv(pdf_path: str) -> str:
    """
    Lee y analiza un CV en formato PDF. Extrae: nombre, título, skills técnicos y blandos,
    idiomas, educación, sectores, roles objetivo y keywords para búsqueda en LinkedIn.
    Usá esta herramienta antes de buscar empleos para que la búsqueda sea precisa.

    Args:
        pdf_path: Ruta absoluta al archivo PDF del CV (ej: /home/usuario/mi_cv.pdf)
    """
    global _cv_cache

    try:
        client, _, _ = _get_clients()
    except ValueError as e:
        return f"❌ Error de configuración: {e}"

    try:
        cv_text = _extract_pdf_text(pdf_path)
    except (FileNotFoundError, ValueError) as e:
        return f"❌ Error al leer PDF: {e}"

    try:
        cv_info = _analyze_cv_with_claude(client, cv_text)
    except Exception as e:
        return f"❌ Error al analizar con Claude: {e}"

    _cv_cache[pdf_path] = {"info": cv_info, "text": cv_text[:5000]}

    skills = ", ".join(cv_info.get("skills_tecnicos", [])[:10])
    roles  = ", ".join(cv_info.get("roles_objetivo", []))
    idiomas = ", ".join(cv_info.get("idiomas", []))
    keywords = ", ".join(cv_info.get("keywords_busqueda", []))

    return f"""✅ CV analizado correctamente.

**{cv_info.get('nombre', 'N/A')}** — {cv_info.get('titulo_actual', 'N/A')}
Experiencia: {cv_info.get('años_experiencia', '?')} años
Ubicación: {cv_info.get('ubicacion', 'No especificada')}
Educación: {cv_info.get('educacion', 'N/A')}
Idiomas: {idiomas}

**Skills técnicos:** {skills}

**Resumen:** {cv_info.get('resumen', '')}

**Roles objetivo para búsqueda:**
{chr(10).join(f'  • {r}' for r in cv_info.get('roles_objetivo', []))}

**Keywords de búsqueda sugeridas:** {keywords}

ℹ️ CV cacheado en memoria. Ahora podés usar `buscar_empleos` o `pipeline_completo`."""


@mcp.tool()
def buscar_empleos(
    pdf_path: str,
    location: str = "Argentina",
    keywords: str = "",
    limit: int = 20,
    min_score: int = 55,
) -> str:
    """
    Busca empleos en LinkedIn que se ajusten al CV y los rankea con IA.
    Genera un reporte HTML con los mejores matches.
    Si el CV no fue analizado previamente, lo analiza automáticamente.

    Args:
        pdf_path: Ruta al PDF del CV
        location: Ubicación para filtrar empleos (ej: "Argentina", "Buenos Aires", "Remote", "España")
        keywords: Keywords adicionales de búsqueda (ej: "DevSecOps Python", "Security Engineer")
        limit: Cantidad máxima de empleos a buscar (default: 20, máx recomendado: 50)
        min_score: Score mínimo de compatibilidad para incluir en resultados (0-100, default: 55)
    """
    global _cv_cache

    try:
        client, li_email, li_pass = _get_clients()
    except ValueError as e:
        return f"❌ Error de configuración: {e}"

    # Obtener o analizar CV
    if pdf_path not in _cv_cache:
        try:
            cv_text = _extract_pdf_text(pdf_path)
            cv_info = _analyze_cv_with_claude(client, cv_text)
            _cv_cache[pdf_path] = {"info": cv_info, "text": cv_text[:5000]}
        except Exception as e:
            return f"❌ Error al procesar el CV: {e}"
    else:
        cv_info = _cv_cache[pdf_path]["info"]

    # Conectar a LinkedIn
    try:
        li = Linkedin(li_email, li_pass)
    except Exception as e:
        return f"❌ Error al autenticar en LinkedIn: {e}\nVerificá tus credenciales o si LinkedIn pidió verificación manual."

    # Construir queries
    queries = []
    for role in cv_info.get("roles_objetivo", [])[:3]:
        queries.append({"keywords": role, "location": location})
    kws = cv_info.get("keywords_busqueda", [])
    if kws:
        queries.append({"keywords": " ".join(kws[:4]), "location": location})
    if keywords:
        queries.append({"keywords": keywords, "location": location})
    if not queries:
        queries.append({"keywords": cv_info.get("titulo_actual", "developer"), "location": location})

    # Buscar jobs
    jobs = _search_jobs(li, queries, min(limit, 50))
    if not jobs:
        return f"❌ No se encontraron empleos para las queries generadas.\nProbá con `keywords` más específicos o cambiá la `location`."

    # Scorear
    to_score = jobs[:MAX_SCORE_JOBS]
    scored   = []
    for job in to_score:
        sd = _score_job(client, cv_info, job)
        job["score_data"] = sd
        scored.append(job)
        time.sleep(0.1)

    # Generar HTML
    output_path = Path(pdf_path).stem + "_matches.html"
    try:
        _generate_html(cv_info, scored, output_path, min_score)
        html_msg = f"\n\n📄 **Reporte HTML:** `{output_path}`\n  → Abrilo con: `xdg-open {output_path}`"
    except Exception as e:
        html_msg = f"\n\n⚠️ No se pudo generar el HTML: {e}"

    # Armar respuesta en markdown
    ranked = sorted(scored, key=lambda x: x.get("score_data", {}).get("score", 0), reverse=True)
    top    = [j for j in ranked if j.get("score_data", {}).get("score", 0) >= min_score]

    if not top:
        return f"Se analizaron {len(scored)} ofertas pero ninguna superó el score mínimo de {min_score}.\nProbá bajando `min_score` a 40.{html_msg}"

    lines = [
        f"## 🏆 Top {min(len(top), 10)} matches para {cv_info.get('nombre','tu perfil')}",
        f"*{len(scored)} ofertas analizadas · {len(top)} con score ≥ {min_score} · Ubicación: {location}*\n",
    ]

    for rank, job in enumerate(top[:10], 1):
        sd    = job.get("score_data", {})
        score = sd.get("score", 0)
        bar   = "█" * (score // 10) + "░" * (10 - score // 10)
        skills_ok   = " · ".join(sd.get("skills_coincidentes", [])[:5]) or "—"
        url_txt     = f"\n  🔗 {job['url']}" if job.get("url") else ""
        lines += [
            f"### #{rank} — {job['title']}",
            f"**{job['company']}** · {job.get('location','N/A')} · {job.get('workplace','')}",
            f"**Score: {score}/100** `{bar}` — {sd.get('match_nivel','')}",
            f"✅ Skills: {skills_ok}",
            f"💬 {sd.get('recomendacion','')}",
            f"{url_txt}",
            "",
        ]

    lines.append(html_msg)
    return "\n".join(lines)


@mcp.tool()
def pipeline_completo(
    pdf_path: str,
    location: str = "Argentina",
    keywords: str = "",
    limit: int = 25,
    min_score: int = 55,
    output_html: str = "",
) -> str:
    """
    Ejecuta el pipeline completo en un solo paso: lee el CV, busca empleos en LinkedIn,
    los rankea con IA y genera un reporte HTML. Ideal para usar por primera vez.

    Args:
        pdf_path: Ruta al PDF del CV
        location: Ubicación (ej: "Argentina", "Buenos Aires", "España", "Remote")
        keywords: Keywords adicionales (ej: "Python DevSecOps", "Cloud Security")
        limit: Máximo de empleos a buscar (default: 25)
        min_score: Score mínimo para incluir en reporte (0-100, default: 55)
        output_html: Ruta del HTML de salida (opcional, se genera automáticamente si no se indica)
    """
    global _cv_cache
    _cv_cache.pop(pdf_path, None)  # Forzar re-análisis del CV
    return buscar_empleos(pdf_path, location, keywords, limit, min_score)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

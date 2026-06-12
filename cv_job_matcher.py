#!/usr/bin/env python3
"""
CV Job Matcher — LinkedIn Agent
Lee tu CV en PDF, extrae tus skills con Claude y busca los trabajos
más afines en LinkedIn. Genera un reporte HTML rankeado.

Uso:
    python3 cv_job_matcher.py --cv tu_cv.pdf --location "Argentina" --limit 30
    python3 cv_job_matcher.py --cv tu_cv.pdf --keywords "DevSecOps Python" --limit 20
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pdfplumber
import anthropic
from linkedin_api import Linkedin
from dotenv import load_dotenv

load_dotenv()

# ─── Configuración ───────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LINKEDIN_EMAIL    = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.environ.get("LINKEDIN_PASSWORD", "")

MODEL = "claude-sonnet-4-6"
MAX_JOBS_TO_SCORE = 40      # Máximo de empleos que se van a scorear con Claude
MIN_SCORE_SHOW    = 50      # Score mínimo para incluir en el reporte (0-100)
RATE_LIMIT_SECS   = 0.5     # Pausa entre llamadas a LinkedIn


# ─── PDF ─────────────────────────────────────────────────────────────────────

def extract_cv_text(pdf_path: str) -> str:
    """Extrae todo el texto del PDF del CV."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    full_text = "\n".join(text_parts)
    if not full_text.strip():
        raise ValueError(f"No se pudo extraer texto del PDF: {pdf_path}")
    return full_text


# ─── Claude: Análisis del CV ─────────────────────────────────────────────────

def analyze_cv(client: anthropic.Anthropic, cv_text: str) -> dict:
    """Usa Claude para extraer información estructurada del CV."""
    print("  Analizando CV con Claude...", flush=True)

    prompt = f"""Analiza el siguiente CV y extrae la información en formato JSON.
Responde SOLO con el JSON, sin texto adicional ni bloques de código.

CV:
{cv_text[:12000]}

Devuelve exactamente este JSON:
{{
  "nombre": "nombre completo",
  "titulo_actual": "título o rol principal",
  "años_experiencia": 0,
  "resumen": "2-3 oraciones del perfil profesional",
  "skills_tecnicos": ["lista", "de", "tecnologías", "y", "herramientas"],
  "skills_blandos": ["liderazgo", "comunicación", etc],
  "idiomas": ["Español - nativo", "Inglés - avanzado"],
  "educacion": "título más alto obtenido",
  "sectores": ["industrias o sectores en los que trabajó"],
  "roles_objetivo": ["3-5 títulos de trabajo que encajan con el perfil"],
  "keywords_busqueda": ["5-8 términos para buscar en LinkedIn en el idioma del CV"],
  "ubicacion": "ciudad o región si está en el CV"
}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    # Remover bloques de código si los hay
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Intentar extraer JSON con regex
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Claude no retornó JSON válido:\n{raw[:500]}")


# ─── LinkedIn: Búsqueda de Empleos ───────────────────────────────────────────

def build_search_queries(cv_info: dict, extra_keywords: str = "", location: str = "") -> list[dict]:
    """Construye queries de búsqueda a partir de la info del CV."""
    loc = location or cv_info.get("ubicacion", "")
    queries = []

    # Queries basadas en roles objetivo
    for role in cv_info.get("roles_objetivo", [])[:3]:
        queries.append({"keywords": role, "location": loc})

    # Queries con keywords del CV
    kws = cv_info.get("keywords_busqueda", [])
    if kws:
        combined = " ".join(kws[:4])
        queries.append({"keywords": combined, "location": loc})

    # Query extra si el usuario especificó
    if extra_keywords:
        queries.append({"keywords": extra_keywords, "location": loc})

    # Fallback con el título actual
    if not queries:
        titulo = cv_info.get("titulo_actual", "developer")
        queries.append({"keywords": titulo, "location": loc})

    return queries


def search_linkedin_jobs(li: Linkedin, queries: list[dict], limit: int = 30) -> list[dict]:
    """Ejecuta las queries en LinkedIn y devuelve lista deduplicada de jobs."""
    seen_ids = set()
    all_jobs = []

    per_query = max(10, limit // len(queries)) if queries else limit

    for q in queries:
        kw = q.get("keywords", "")
        loc = q.get("location", "")
        print(f"  Buscando: \"{kw}\" en \"{loc or 'cualquier lugar'}\"...", flush=True)

        try:
            results = li.search_jobs(
                keywords=kw,
                location_name=loc if loc else None,
                limit=per_query,
            )
        except Exception as e:
            print(f"  [!] Error en búsqueda LinkedIn: {e}", flush=True)
            continue

        for job in results:
            job_id = job.get("entityUrn", "") or job.get("trackingUrn", "") or str(job)
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            # Obtener detalles del job
            try:
                detail_id = job_id.split(":")[-1]
                details = li.get_job(detail_id) if detail_id.isdigit() else {}
            except Exception:
                details = {}

            # Extraer campos relevantes
            title = (
                job.get("title") or
                details.get("title") or
                job.get("trackingUrn", "Sin título")
            )
            company = (
                job.get("companyName") or
                details.get("companyDetails", {}).get("companyResolutionResult", {}).get("name", "") or
                "Empresa desconocida"
            )
            location_str = (
                details.get("formattedLocation") or
                job.get("formattedLocation") or ""
            )
            description = (
                details.get("description", {}).get("text", "") or
                details.get("description") or ""
                if isinstance(details.get("description"), str) else ""
            )
            job_url = f"https://www.linkedin.com/jobs/view/{detail_id}" if detail_id.isdigit() else ""
            workplace = details.get("workplaceTypes", [""])[0] if details.get("workplaceTypes") else ""
            seniority = details.get("expLevel", "")

            all_jobs.append({
                "id": detail_id,
                "title": str(title),
                "company": str(company),
                "location": str(location_str),
                "workplace": str(workplace),
                "seniority": str(seniority),
                "description": str(description)[:3000],
                "url": job_url,
                "query": kw,
            })

            time.sleep(RATE_LIMIT_SECS)

        if len(all_jobs) >= limit:
            break

    return all_jobs[:limit]


# ─── Claude: Scoring ─────────────────────────────────────────────────────────

def score_job(client: anthropic.Anthropic, cv_info: dict, cv_text_short: str, job: dict) -> dict:
    """Usa Claude para evaluar qué tan bien se ajusta el trabajo al CV."""

    cv_summary = json.dumps({
        "nombre": cv_info.get("nombre"),
        "titulo": cv_info.get("titulo_actual"),
        "años_exp": cv_info.get("años_experiencia"),
        "skills": cv_info.get("skills_tecnicos", [])[:20],
        "idiomas": cv_info.get("idiomas", []),
        "sectores": cv_info.get("sectores", []),
    }, ensure_ascii=False)

    job_info = f"""Título: {job['title']}
Empresa: {job['company']}
Ubicación: {job['location']}
Modalidad: {job['workplace']}
Seniority: {job['seniority']}
Descripción:
{job['description'][:2000]}"""

    prompt = f"""Evalúa qué tan bien se ajusta esta oferta de trabajo al perfil del candidato.

PERFIL DEL CANDIDATO:
{cv_summary}

OFERTA DE TRABAJO:
{job_info}

Responde SOLO con JSON sin texto adicional:
{{
  "score": 85,
  "match_nivel": "Excelente|Muy bueno|Bueno|Regular|Bajo",
  "skills_coincidentes": ["skill1", "skill2"],
  "skills_faltantes": ["skill3"],
  "puntos_fuertes": "2-3 oraciones por qué es buen match",
  "puntos_debiles": "1-2 oraciones sobre gaps",
  "recomendacion": "una oración de recomendación personalizada"
}}

El score va de 0 a 100. Sé preciso y objetivo."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*", "", raw)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        pass

    return {
        "score": 0,
        "match_nivel": "Error",
        "skills_coincidentes": [],
        "skills_faltantes": [],
        "puntos_fuertes": "No se pudo evaluar.",
        "puntos_debiles": "",
        "recomendacion": ""
    }


# ─── HTML Report ─────────────────────────────────────────────────────────────

def generate_html_report(cv_info: dict, scored_jobs: list[dict], output_path: str):
    """Genera el reporte HTML con los resultados rankeados."""

    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    nombre = cv_info.get("nombre", "Candidato")
    titulo = cv_info.get("titulo_actual", "")

    # Ordenar por score
    ranked = sorted(scored_jobs, key=lambda x: x.get("score_data", {}).get("score", 0), reverse=True)
    top = [j for j in ranked if j.get("score_data", {}).get("score", 0) >= MIN_SCORE_SHOW]

    def score_color(score):
        if score >= 80: return "#30d158"
        if score >= 65: return "#ffd60a"
        if score >= 50: return "#ff9500"
        return "#ff453a"

    def score_bg(score):
        if score >= 80: return "#00200a"
        if score >= 65: return "#2d2700"
        if score >= 50: return "#2d1a00"
        return "#3d0000"

    def nivel_badge(nivel):
        colors = {
            "Excelente": ("var(--low)", "var(--low-bg)"),
            "Muy bueno": ("#7ac96a", "#0a2000"),
            "Bueno": ("var(--medium)", "var(--medium-bg)"),
            "Regular": ("var(--high)", "var(--high-bg)"),
            "Bajo": ("var(--critical)", "var(--critical-bg)"),
        }
        c, bg = colors.get(nivel, ("#8b949e", "#161b22"))
        return f'<span style="background:{bg};color:{c};border:1px solid {c};border-radius:12px;padding:2px 10px;font-size:11px;font-weight:600;">{nivel}</span>'

    cards_html = ""
    for rank, job in enumerate(top, 1):
        sd = job.get("score_data", {})
        score = sd.get("score", 0)
        sc = score_color(score)
        sb = score_bg(score)
        skills_ok = "".join(
            f'<span style="background:#002800;color:#3fb950;border:1px solid #3fb950;border-radius:4px;padding:1px 8px;font-size:11px;margin:2px;display:inline-block;">{s}</span>'
            for s in sd.get("skills_coincidentes", [])[:8]
        )
        skills_missing = "".join(
            f'<span style="background:#3d0000;color:#ff7b72;border:1px solid #ff7b72;border-radius:4px;padding:1px 8px;font-size:11px;margin:2px;display:inline-block;">{s}</span>'
            for s in sd.get("skills_faltantes", [])[:5]
        )
        url_btn = f'<a href="{job["url"]}" target="_blank" style="display:inline-block;background:#1a3a5c;color:#58a6ff;border:1px solid #58a6ff;border-radius:6px;padding:6px 16px;font-size:13px;font-weight:600;text-decoration:none;margin-top:12px;">Ver en LinkedIn →</a>' if job.get("url") else ""
        workplace_tag = f'<span style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:1px 8px;font-size:11px;color:#8b949e;margin-left:6px;">{job["workplace"]}</span>' if job.get("workplace") else ""

        cards_html += f"""
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;margin-bottom:20px;overflow:hidden;">
      <div style="display:flex;align-items:flex-start;gap:20px;padding:20px 24px;border-bottom:1px solid #30363d;">
        <div style="min-width:72px;text-align:center;">
          <div style="font-size:11px;color:#8b949e;margin-bottom:2px;">#{rank}</div>
          <div style="font-size:42px;font-weight:800;font-family:monospace;color:{sc};line-height:1;background:{sb};border:2px solid {sc};border-radius:10px;padding:6px 10px;">{score}</div>
          <div style="font-size:10px;color:{sc};margin-top:4px;font-weight:600;">/100</div>
        </div>
        <div style="flex:1;">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
            <h3 style="font-size:17px;font-weight:700;color:#fff;margin:0;">{job['title']}</h3>
            {nivel_badge(sd.get('match_nivel',''))}
            {workplace_tag}
          </div>
          <div style="font-size:14px;color:#58a6ff;margin-bottom:4px;">🏢 {job['company']}</div>
          <div style="font-size:13px;color:#8b949e;">📍 {job['location'] or 'No especificada'} &nbsp;|&nbsp; 🔍 Query: <em>{job.get('query','')}</em></div>
        </div>
      </div>
      <div style="padding:18px 24px;">
        {'<div style="margin-bottom:12px;"><div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8b949e;margin-bottom:6px;font-weight:600;">Skills coincidentes</div>' + skills_ok + '</div>' if skills_ok else ''}
        {'<div style="margin-bottom:12px;"><div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8b949e;margin-bottom:6px;font-weight:600;">Skills faltantes / a desarrollar</div>' + skills_missing + '</div>' if skills_missing else ''}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px;">
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;">
            <div style="font-size:11px;color:#3fb950;font-weight:600;margin-bottom:4px;">✓ Puntos fuertes</div>
            <div style="font-size:13px;color:#c9d1d9;">{sd.get('puntos_fuertes','')}</div>
          </div>
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;">
            <div style="font-size:11px;color:#ff9500;font-weight:600;margin-bottom:4px;">△ Puntos a considerar</div>
            <div style="font-size:13px;color:#c9d1d9;">{sd.get('puntos_debiles','') or '—'}</div>
          </div>
        </div>
        {'<div style="background:#1a1a2e;border-left:3px solid #58a6ff;border-radius:0 6px 6px 0;padding:10px 14px;font-size:13px;color:#a5d6ff;margin-bottom:4px;"><strong>💡 Recomendación:</strong> ' + sd.get("recomendacion","") + '</div>' if sd.get("recomendacion") else ''}
        {url_btn}
      </div>
    </div>"""

    skills_list = " ".join(
        f'<span style="background:#21262d;border:1px solid #30363d;border-radius:4px;padding:2px 8px;font-size:12px;margin:2px;display:inline-block;color:#c9d1d9;">{s}</span>'
        for s in cv_info.get("skills_tecnicos", [])[:15]
    )
    roles_list = " | ".join(cv_info.get("roles_objetivo", []))

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CV Match — {nombre}</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d; --text: #c9d1d9;
    --text-dim: #8b949e; --accent: #58a6ff; --critical: #ff453a;
    --critical-bg: #3d0000; --high: #ff9500; --high-bg: #2d1a00;
    --medium: #ffd60a; --medium-bg: #2d2700; --low: #30d158; --low-bg: #00200a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; font-size: 14px; }}
  .page {{ max-width: 1000px; margin: 0 auto; padding: 32px 20px; }}
  @media (max-width: 600px) {{ .two-col {{ grid-template-columns: 1fr !important; }} }}
</style>
</head>
<body>
<div class="page">

  <div style="background:linear-gradient(135deg,#0d1117,#161b22,#0a1628);border:1px solid #30363d;border-radius:12px;padding:32px;margin-bottom:28px;">
    <div style="font-size:13px;color:#8b949e;margin-bottom:8px;">Reporte de Matching CV ↔ LinkedIn — {fecha}</div>
    <h1 style="font-size:26px;font-weight:700;color:#fff;margin-bottom:4px;">{nombre}</h1>
    <div style="font-size:16px;color:#58a6ff;margin-bottom:16px;">{titulo}</div>
    <div style="margin-bottom:12px;">{skills_list}</div>
    <div style="font-size:12px;color:#8b949e;">Roles buscados: <span style="color:#c9d1d9;">{roles_list}</span></div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:20px;">
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center;">
        <div style="font-size:28px;font-weight:800;color:#fff;">{len(scored_jobs)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;">Ofertas analizadas</div>
      </div>
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center;">
        <div style="font-size:28px;font-weight:800;color:#30d158;">{len(top)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;">Con score ≥ {MIN_SCORE_SHOW}</div>
      </div>
      <div style="background:rgba(255,255,255,.04);border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center;">
        <div style="font-size:28px;font-weight:800;color:#ffd60a;">{max((j.get('score_data',{}).get('score',0) for j in scored_jobs), default=0)}</div>
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;">Score máximo</div>
      </div>
    </div>
  </div>

  <div style="margin-bottom:28px;">
    <div style="font-size:18px;font-weight:700;color:#fff;border-bottom:1px solid #30363d;padding-bottom:12px;margin-bottom:20px;">
      🏆 Ofertas más afines a tu perfil
      <span style="font-size:13px;font-weight:400;color:#8b949e;margin-left:8px;">ordenadas por score de compatibilidad</span>
    </div>
    {cards_html if cards_html else '<div style="color:#8b949e;padding:20px;text-align:center;">No se encontraron ofertas con score suficiente.</div>'}
  </div>

  <div style="border-top:1px solid #30363d;padding-top:16px;color:#8b949e;font-size:12px;text-align:center;">
    Generado por CV Job Matcher · {fecha} · Modelo: {MODEL}
  </div>
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  Reporte guardado: {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global MIN_SCORE_SHOW

    parser = argparse.ArgumentParser(description="CV Job Matcher — LinkedIn + Claude AI")
    parser.add_argument("--cv",       required=True,  help="Ruta al PDF del CV")
    parser.add_argument("--location", default="",     help='Ubicacion (ej: "Argentina", "Buenos Aires")')
    parser.add_argument("--keywords", default="",     help='Keywords extra de busqueda (ej: "DevSecOps Python")')
    parser.add_argument("--limit",    type=int, default=30, help="Maximo de empleos a buscar (default: 30)")
    parser.add_argument("--output",   default="",     help="Ruta del HTML de salida (default: cv_matches.html)")
    parser.add_argument("--min-score",type=int, default=MIN_SCORE_SHOW, help=f"Score minimo para mostrar (default: {MIN_SCORE_SHOW})")
    args = parser.parse_args()

    MIN_SCORE_SHOW = args.min_score

    # Validar credenciales
    api_key    = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    li_email   = LINKEDIN_EMAIL    or os.environ.get("LINKEDIN_EMAIL", "")
    li_pass    = LINKEDIN_PASSWORD or os.environ.get("LINKEDIN_PASSWORD", "")

    if not api_key:
        print("ERROR: Falta ANTHROPIC_API_KEY. Exportala con:\n  export ANTHROPIC_API_KEY=tu_clave")
        sys.exit(1)
    if not li_email or not li_pass:
        print("ERROR: Faltan credenciales de LinkedIn.\n  export LINKEDIN_EMAIL=tu@email.com\n  export LINKEDIN_PASSWORD=tu_contraseña")
        sys.exit(1)
    if not Path(args.cv).exists():
        print(f"ERROR: No se encontró el archivo PDF: {args.cv}")
        sys.exit(1)

    output_path = args.output or Path(args.cv).stem + "_matches.html"

    print(f"\n{'='*55}")
    print("  CV JOB MATCHER — LinkedIn + Claude AI")
    print(f"{'='*55}")
    print(f"  CV:       {args.cv}")
    print(f"  Location: {args.location or 'Sin filtro'}")
    print(f"  Límite:   {args.limit} empleos")
    print(f"  Output:   {output_path}")
    print(f"{'='*55}\n")

    client = anthropic.Anthropic(api_key=api_key)

    # 1. Leer PDF
    print("[1/5] Extrayendo texto del CV...")
    cv_text = extract_cv_text(args.cv)
    print(f"  {len(cv_text)} caracteres extraídos.")

    # 2. Analizar CV
    print("\n[2/5] Analizando CV con Claude...")
    cv_info = analyze_cv(client, cv_text)
    print(f"  Nombre: {cv_info.get('nombre','?')}")
    print(f"  Título: {cv_info.get('titulo_actual','?')}")
    print(f"  Skills: {', '.join(cv_info.get('skills_tecnicos',[])[:6])}...")
    print(f"  Roles objetivo: {', '.join(cv_info.get('roles_objetivo',[]))}")

    # 3. Conectar a LinkedIn
    print("\n[3/5] Conectando a LinkedIn...")
    try:
        li = Linkedin(li_email, li_pass)
        print("  Autenticado correctamente.")
    except Exception as e:
        print(f"  ERROR al autenticar en LinkedIn: {e}")
        print("  Verificá tus credenciales o si LinkedIn bloqueó el acceso temporalmente.")
        sys.exit(1)

    # 4. Buscar empleos
    print("\n[4/5] Buscando empleos en LinkedIn...")
    queries = build_search_queries(cv_info, args.keywords, args.location)
    jobs = search_linkedin_jobs(li, queries, args.limit)
    print(f"  {len(jobs)} ofertas únicas encontradas.")

    if not jobs:
        print("  No se encontraron ofertas. Probá con --keywords o --location más amplios.")
        sys.exit(0)

    # 5. Scorear con Claude
    print(f"\n[5/5] Evaluando compatibilidad con Claude (máx {MAX_JOBS_TO_SCORE} ofertas)...")
    jobs_to_score = jobs[:MAX_JOBS_TO_SCORE]
    cv_short = f"Skills: {cv_info.get('skills_tecnicos',[])} | Años exp: {cv_info.get('años_experiencia')} | Título: {cv_info.get('titulo_actual')}"

    scored = []
    for i, job in enumerate(jobs_to_score, 1):
        title = job['title'][:50]
        company = job['company'][:30]
        print(f"  [{i:2d}/{len(jobs_to_score)}] {title} @ {company}", flush=True)
        sd = score_job(client, cv_info, cv_short, job)
        job["score_data"] = sd
        scored.append(job)
        time.sleep(0.1)  # Rate limit Claude

    # Ordenar y mostrar top 5 en consola
    top5 = sorted(scored, key=lambda x: x.get("score_data",{}).get("score",0), reverse=True)[:5]
    print(f"\n{'='*55}")
    print("  TOP 5 MATCHES:")
    print(f"{'='*55}")
    for rank, j in enumerate(top5, 1):
        sc = j.get("score_data",{}).get("score",0)
        print(f"  #{rank}  [{sc:3d}/100]  {j['title'][:40]} @ {j['company'][:25]}")
    print(f"{'='*55}\n")

    # 6. Generar HTML
    generate_html_report(cv_info, scored, output_path)
    print(f"  ✓ Listo. Abrí el reporte en el navegador:")
    print(f"    firefox {output_path}")
    print(f"    # o: xdg-open {output_path}\n")


if __name__ == "__main__":
    main()

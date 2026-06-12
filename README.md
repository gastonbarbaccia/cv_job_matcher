# CV Job Matcher

Busca empleos en LinkedIn que se ajusten a tu CV usando IA. Lee el PDF de tu CV, extrae tus skills y experiencia con Claude, busca ofertas en LinkedIn con tu cuenta y genera un reporte HTML rankeado con score de compatibilidad por cada oferta.

Disponible en dos modos: **script de línea de comandos** y **servidor MCP** para usar directamente desde Claude Code.

---

## Requisitos

- Python 3.10+
- Cuenta de LinkedIn
- API Key de Anthropic → [console.anthropic.com](https://console.anthropic.com/settings/api-keys)

```bash
pip3 install pdfplumber anthropic linkedin-api "mcp[cli]" python-dotenv --break-system-packages
```

---

## Modo 1 — MCP (recomendado)

Se integra con Claude Code. Podés pedirle directamente en el chat que busque empleos para tu CV.

### Registrar el servidor

```bash
claude mcp add cv-job-matcher \
  --scope user \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e LINKEDIN_EMAIL="tu@email.com" \
  -e LINKEDIN_PASSWORD="tu_contraseña" \
  -- python3 /home/gastonbarbaccia/cv_job_matcher/cv_matcher_mcp.py
```

Reiniciá Claude Code. Verificar que conectó:

```bash
claude mcp list
# cv-job-matcher: ... ✔ Connected
```

### Uso en el chat

```
busca trabajos para mi CV en /ruta/a/mi_cv.pdf, ubicación Argentina
```

```
analizá mi CV en ~/Documentos/cv.pdf y buscame empleos remotos de DevSecOps
```

```
usá pipeline_completo con mi CV en /home/.../cv.pdf, ubicación España, keywords "cloud security python", limit 30
```

### Tools disponibles

| Tool | Descripción | Parámetros clave |
|------|-------------|-----------------|
| `leer_cv` | Analiza el PDF y extrae skills, roles objetivo, keywords sugeridas | `pdf_path` |
| `buscar_empleos` | Busca en LinkedIn + rankea con IA + genera HTML | `pdf_path`, `location`, `keywords`, `limit`, `min_score` |
| `pipeline_completo` | Hace todo en un paso desde cero (fuerza re-análisis del CV) | igual que `buscar_empleos` + `output_html` |

El CV se cachea en memoria durante la sesión. Llamar `buscar_empleos` dos veces con distintas ubicaciones no reprocesa el PDF.

### Actualizar credenciales

```bash
claude mcp remove "cv-job-matcher" -s user

claude mcp add cv-job-matcher \
  --scope user \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e LINKEDIN_EMAIL="nueva@email.com" \
  -e LINKEDIN_PASSWORD="nueva_contraseña" \
  -- python3 /home/gastonbarbaccia/cv_job_matcher/cv_matcher_mcp.py
```

---

## Modo 2 — Script de línea de comandos

### Configurar credenciales con `.env` (recomendado)

Copiá el archivo de ejemplo y completá tus credenciales:

```bash
cp .env.example .env
```

Editá `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
LINKEDIN_EMAIL=tu@email.com
LINKEDIN_PASSWORD=tu_contraseña
```

El script lo carga automáticamente al ejecutarse. No necesitás exportar nada.

> **Nota:** nunca subas `.env` a un repositorio. Está incluido en `.gitignore` por convención.

### Alternativa — variables de entorno manuales

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export LINKEDIN_EMAIL="tu@email.com"
export LINKEDIN_PASSWORD="tu_contraseña"
```

Para no repetirlo cada vez agregalo a `~/.bashrc`:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
```

### Ejecutar

```bash
# Búsqueda básica
python3 cv_job_matcher.py --cv mi_cv.pdf --location "Argentina"

# Con keywords adicionales y más resultados
python3 cv_job_matcher.py --cv mi_cv.pdf --keywords "DevSecOps Python" --limit 50

# Buenos Aires, umbral de match más alto
python3 cv_job_matcher.py --cv mi_cv.pdf --location "Buenos Aires" --min-score 65

# Búsqueda remota global
python3 cv_job_matcher.py --cv mi_cv.pdf --location "Remote" --keywords "Security Engineer"

# Guardar reporte con nombre específico
python3 cv_job_matcher.py --cv mi_cv.pdf --output matches_julio_2026.html
```

### Parámetros

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `--cv` | requerido | Ruta al PDF del CV |
| `--location` | sin filtro | Ciudad o país (ej: `"Argentina"`, `"Remote"`) |
| `--keywords` | — | Keywords extra para agregar a la búsqueda |
| `--limit` | `30` | Máximo de empleos a buscar |
| `--min-score` | `50` | Score mínimo para aparecer en el reporte (0–100) |
| `--output` | `{cv}_matches.html` | Nombre del HTML de salida |

### Ver el reporte

```bash
xdg-open mi_cv_matches.html
```

---

## Cómo funciona

```
PDF del CV
    │
    ▼
pdfplumber extrae el texto
    │
    ▼
Claude analiza el CV
(skills, experiencia, roles objetivo, keywords de búsqueda)
    │
    ▼
LinkedIn API busca empleos
(múltiples queries, deduplicadas)
    │
    ▼
Claude evalúa cada oferta vs. el CV
(score 0–100, skills coincidentes, gaps, recomendación)
    │
    ▼
Reporte HTML rankeado
(links directos a LinkedIn, dark theme)
```

---

## Reporte HTML

Cada oferta incluye:

- **Score** de compatibilidad (0–100) con nivel: Excelente / Muy bueno / Bueno / Regular / Bajo
- **Skills coincidentes** entre el CV y la oferta
- **Skills a desarrollar** (gaps identificados)
- **Puntos fuertes** del match
- **Recomendación personalizada** de Claude
- **Link directo** a la oferta en LinkedIn

---

## Archivos

```
cv_job_matcher/
├── cv_matcher_mcp.py    # Servidor MCP (para Claude Code)
├── cv_job_matcher.py    # Script CLI (línea de comandos)
├── requirements.txt     # Dependencias
├── .env                 # Credenciales locales (no commitear)
├── .env.example         # Plantilla de credenciales
└── README.md
```

---

## Troubleshooting

**LinkedIn pide verificación al autenticar**
Iniciá sesión manualmente en el navegador, resolvé la verificación y reintentá.

**Error `ANTHROPIC_API_KEY` no encontrada (modo MCP)**
Las credenciales se pasan con `-e` al registrar el servidor. Si las cambiaste, remové y volvé a agregar el servidor con `claude mcp remove` + `claude mcp add`.

**No se encontraron empleos**
Probá con `keywords` más amplios o cambiá `location` a `"Remote"` o al país sin ciudad.

**Score siempre bajo**
Bajá `min_score` a `40` y revisá que el PDF del CV tenga texto seleccionable (no escaneado como imagen).

# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Extractor puro (Fase 3.1).

Convierte un PDF (o tablas pre-extraídas) en una lista de RawRow:
estructuras con `parametro`, `valor`, `rango`, `estado_raw`, metadata
de posición (página, tabla_idx, row_idx) y el contexto agregado de la
tabla (`table_context_norm`) para que el classifier resuelva
parámetros ambiguos.

PRINCIPIOS:
  * Función pura: no toca Odoo, no toca BD, no llama IA.
  * Robusto contra texto sucio del PDF: newlines mid-cell, NBSP,
    zero-width, espacios múltiples, columnas desordenadas.
  * Sin reglas clínicas: clasificar es trabajo del classifier.

Salida de extract():
    Una lista de RawRow. Cada RawRow es un dict con shape estable.
"""
import base64
import io
import logging
import re

from .taxonomy import normalize_text


_logger = logging.getLogger(__name__)


# =====================================================================
# Regex compartidos
# =====================================================================

# Detecta un valor numérico simple, posiblemente con signo y separador
# decimal (coma o punto).
_RE_NUMBER = re.compile(r'-?\d+(?:[.,]\d+)?')

# Detecta un RANGO numérico:
#   * "70-110", "70 - 110", "0,431 - 1,329", "0.5 - 4.5"
#   * "<150", "<= 150"
#   * ">30", ">= 30", "≤ X", "≥ X"
#   * "X a Y", "X to Y"
_RE_RANGE = re.compile(
    r'(?:'
    r'(?P<lo1>-?\d+(?:[.,]\d+)?)\s*(?:-|–|—|a|to)\s*(?P<hi1>-?\d+(?:[.,]\d+)?)'
    r'|<\s*=?\s*(?P<hi2>-?\d+(?:[.,]\d+)?)'
    r'|>\s*=?\s*(?P<lo2>-?\d+(?:[.,]\d+)?)'
    r'|≤\s*(?P<hi3>-?\d+(?:[.,]\d+)?)'
    r'|≥\s*(?P<lo3>-?\d+(?:[.,]\d+)?)'
    r')'
)

# Estados textuales típicos (lista corta y conservadora — el classifier
# normaliza estos contra el catálogo de estados).
_KNOWN_STATES = (
    'optimo', 'excelente', 'normal', 'equilibrado', 'sano', 'dentro de rango',
    'leve', 'levemente alterado', 'ligero',
    'moderado', 'moderadamente alterado',
    'severo', 'severamente alterado', 'alterado',
    'critico', 'grave',
)


# =====================================================================
# Shape estable de salida
# =====================================================================
# RawRow es un dict con estas claves. Se documenta como contrato.
RAW_ROW_KEYS = (
    'parametro_raw',          # string como vino del PDF (con typos)
    'parametro_norm',         # normalize_text(parametro_raw)
    'valor_raw',              # string del valor (puede ser texto)
    'valor_norm',
    'rango_raw',
    'rango_norm',
    'estado_raw',             # estado tal cual aparece (puede ser None)
    'estado_norm',
    'pagina',                 # int (1-indexed)
    'tabla_idx',              # int (0-indexed dentro del PDF)
    'row_idx',                # int (0-indexed dentro de la tabla)
    'section_header',         # string opcional: último header de sección
    'table_context_norm',     # string: concatenación normalizada de TODA la tabla
)


def make_raw_row(parametro_raw='', valor_raw='', rango_raw='',
                 estado_raw=None, pagina=0, tabla_idx=0, row_idx=0,
                 section_header='', table_context_norm=''):
    """Constructor canónico de RawRow."""
    return {
        'parametro_raw': parametro_raw or '',
        'parametro_norm': normalize_text(parametro_raw),
        'valor_raw': valor_raw or '',
        'valor_norm': normalize_text(valor_raw),
        'rango_raw': rango_raw or '',
        'rango_norm': normalize_text(rango_raw),
        'estado_raw': estado_raw,
        'estado_norm': normalize_text(estado_raw) if estado_raw else None,
        'pagina': int(pagina or 0),
        'tabla_idx': int(tabla_idx or 0),
        'row_idx': int(row_idx or 0),
        'section_header': section_header or '',
        'table_context_norm': table_context_norm or '',
    }


# =====================================================================
# Heurísticas de identificación de columnas
# =====================================================================

def _cell_kind(cell_norm):
    """Identifica qué tipo de contenido tiene una celda normalizada.

    Returns:
        'range'  → contiene un rango numérico
        'value'  → contiene un valor numérico solo
        'state'  → es una etiqueta de estado conocida
        'text'   → texto descriptivo (probable parámetro)
        'empty'  → vacío
    """
    if not cell_norm or not cell_norm.strip():
        return 'empty'
    text = cell_norm.strip()
    # Estado (match exacto contra labels conocidos)
    if text in _KNOWN_STATES:
        return 'state'
    # Estado parcial (substring) — segundo intento
    for st in _KNOWN_STATES:
        if st in text and len(text) <= len(st) + 5:
            return 'state'
    # Rango
    if _RE_RANGE.search(text):
        return 'range'
    # Valor numérico puro (toda la celda casi-numérica)
    if _RE_NUMBER.fullmatch(text.replace(' ', '').replace('%', '')):
        return 'value'
    # Texto descriptivo
    return 'text'


def _looks_like_header_row(cells_norm):
    """Detecta filas de encabezado tipo: ['Parámetro', 'Valor', 'Rango', 'Estado']."""
    joined = ' '.join(cells_norm)
    header_words = ('parametro', 'analisis', 'prueba', 'valor', 'medicion',
                    'rango', 'referencia', 'estado', 'resultado',
                    'sistema', 'indicador')
    return sum(1 for w in header_words if w in joined) >= 2


def _is_section_header_row(cells_norm):
    """Heurística rápida: una fila con un único texto largo que empieza por
    "sistema " es un header de sección, no datos. El catálogo de sistemas
    decide si realmente es una sección (en el classifier).

    Aquí sólo identificamos el patrón estructural: 1-2 celdas con texto,
    el resto vacío o trivial.
    """
    non_empty = [c for c in cells_norm if c and c.strip()]
    if len(non_empty) > 2:
        return False
    if not non_empty:
        return False
    first = non_empty[0]
    # Patrón frecuente: "sistema <algo>"
    if first.startswith('sistema '):
        return True
    return False


# =====================================================================
# Extracción de una tabla (lista de listas de celdas) → lista de RawRow
# =====================================================================

def extract_from_table(table_rows, pagina=0, tabla_idx=0):
    """Convierte una tabla (lista de filas, cada fila = lista de celdas)
    en una lista de RawRow.

    Heurísticas:
      * Detecta header rows y los descarta.
      * Detecta section headers ("Sistema X") y los emite como
        section_header para las filas siguientes.
      * Para cada fila de datos, identifica qué celda es parametro,
        valor, rango, estado por su `_cell_kind`. Tolera columnas
        en cualquier orden.
      * Celdas partidas: el parámetro se concatena de TODAS las celdas
        contiguas de tipo 'text' antes de la primera 'value'/'range'.
    """
    if not table_rows:
        return []

    # Pre-normalizar todas las celdas de la tabla
    norm_rows = [
        [normalize_text(str(c) if c is not None else '') for c in row]
        for row in table_rows
    ]
    raw_rows_full = [
        [(str(c) if c is not None else '') for c in row]
        for row in table_rows
    ]

    # Contexto agregado de la tabla: concatenar todas las celdas no-vacías
    table_context_norm = ' '.join(
        c for row in norm_rows for c in row if c and c.strip()
    )

    out = []
    current_section = ''
    for row_idx, (cells_raw, cells_norm) in enumerate(
        zip(raw_rows_full, norm_rows)
    ):
        # 1) ¿Es header de tabla? (descartar)
        if _looks_like_header_row(cells_norm):
            continue
        # 2) ¿Es section header? (actualizar contexto, descartar como dato)
        if _is_section_header_row(cells_norm):
            non_empty = [c for c in cells_norm if c and c.strip()]
            current_section = non_empty[0] if non_empty else ''
            continue
        # 3) Fila de datos
        # Identificar el tipo de cada celda
        kinds = [_cell_kind(c) for c in cells_norm]
        # Si no hay al menos una celda 'text' o 'value', skipear
        if not any(k in ('text', 'value', 'range', 'state') for k in kinds):
            continue
        # Buscar parametro: concatenar texts contiguos hasta primer 'value'/'range'
        parametro_parts_raw = []
        first_data_idx = None
        for i, k in enumerate(kinds):
            if k == 'text':
                parametro_parts_raw.append(cells_raw[i].strip())
            elif k in ('value', 'range', 'state'):
                first_data_idx = i
                break
            elif k == 'empty':
                continue
        # Si NO encontramos data después, intentar usar el primer 'text'
        # como parametro solo
        if not parametro_parts_raw and first_data_idx is None:
            # Sólo había estados o nada útil
            continue
        parametro_raw = ' '.join(p for p in parametro_parts_raw if p).strip()
        # Buscar valor (primera celda de tipo 'value' o 'range' que parezca valor)
        valor_raw = ''
        rango_raw = ''
        estado_raw = None
        for i, k in enumerate(kinds):
            cell = cells_raw[i].strip()
            if k == 'range' and not rango_raw:
                rango_raw = cell
            elif k == 'value' and not valor_raw:
                valor_raw = cell
            elif k == 'state' and not estado_raw:
                estado_raw = cell

        # Si NO hay parametro y SÍ hay valor/rango/estado → fila incompleta,
        # descartar
        if not parametro_raw:
            continue

        out.append(make_raw_row(
            parametro_raw=parametro_raw,
            valor_raw=valor_raw,
            rango_raw=rango_raw,
            estado_raw=estado_raw,
            pagina=pagina,
            tabla_idx=tabla_idx,
            row_idx=row_idx,
            section_header=current_section,
            table_context_norm=table_context_norm,
        ))
    return out


def extract_from_tables(tables_meta):
    """tables_meta es lista de dicts {pagina, tabla_idx, rows} o lista de
    tablas legacy (lista de filas). Devuelve lista plana de RawRow.
    """
    out = []
    for tbl in tables_meta or []:
        if isinstance(tbl, dict):
            pagina = tbl.get('pagina', 0)
            tabla_idx = tbl.get('tabla_idx', 0)
            rows = tbl.get('rows', [])
        else:
            pagina = 0
            tabla_idx = 0
            rows = tbl
        out.extend(extract_from_table(rows, pagina=pagina, tabla_idx=tabla_idx))
    return out


# =====================================================================
# Extracción desde PDF (pdfplumber)
# =====================================================================

def extract_from_pdf_b64(pdf_b64):
    """Abre el PDF con pdfplumber y devuelve (tables_meta, text_total, stats).

    tables_meta: lista de dicts {pagina, tabla_idx, rows}.
    text_total: '\\n' join de page.extract_text() (fallback textual).
    stats: {'paginas': N, 'tablas': N}.

    Para tests sin PDF real, usar extract_from_tables(...) directamente.
    """
    if isinstance(pdf_b64, str):
        data_b64 = pdf_b64.encode('ascii')
    else:
        data_b64 = pdf_b64
    raw = base64.b64decode(data_b64)
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        raise RuntimeError(
            "BioCuántico requiere pdfplumber. Instala: pip3 install pdfplumber"
        )

    tables_meta = []
    text_chunks = []
    n_pages = 0
    global_table_idx = 0
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            n_pages += 1
            try:
                page_tables = page.extract_tables() or []
            except Exception as e:
                _logger.warning("extract_tables falló (p%d): %s", n_pages, e)
                page_tables = []
            for t in page_tables:
                rows = [
                    [('' if c is None else str(c)) for c in row]
                    for row in t
                    if row and any(
                        (c is not None and str(c).strip()) for c in row
                    )
                ]
                if rows:
                    tables_meta.append({
                        'pagina': n_pages,
                        'tabla_idx': global_table_idx,
                        'rows': rows,
                    })
                    global_table_idx += 1
            try:
                txt = page.extract_text() or ''
            except Exception:
                txt = ''
            if txt:
                text_chunks.append(txt)
    return tables_meta, '\n'.join(text_chunks), {
        'paginas': n_pages,
        'tablas': len(tables_meta),
    }


def extract(pdf_b64=None, tables=None, text=None):
    """API principal del extractor.

    Args:
        pdf_b64: base64 del PDF crudo (opcional, preferido).
        tables: lista de tablas pre-extraídas (formato dict-meta o legacy).
        text: texto plano (para fallback sin PDF).

    Returns:
        dict con:
          'rows': lista de RawRow.
          'text_total': str (texto agregado, fallback).
          'stats': dict con paginas, tablas, filas.
    """
    if pdf_b64:
        tables_meta, text_total, pdf_stats = extract_from_pdf_b64(pdf_b64)
        rows = extract_from_tables(tables_meta)
        stats = {
            'paginas': pdf_stats.get('paginas', 0),
            'tablas': pdf_stats.get('tablas', 0),
            'filas': len(rows),
        }
        return {'rows': rows, 'text_total': text_total, 'stats': stats}
    if tables:
        rows = extract_from_tables(tables)
        return {
            'rows': rows,
            'text_total': text or '',
            'stats': {
                'paginas': 0,
                'tablas': len(tables),
                'filas': len(rows),
            },
        }
    # Sin PDF ni tablas: sólo texto plano
    return {
        'rows': [],
        'text_total': text or '',
        'stats': {'paginas': 0, 'tablas': 0, 'filas': 0},
    }

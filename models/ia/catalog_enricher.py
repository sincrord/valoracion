# -*- coding: utf-8 -*-
"""Catalog Enricher — extrae secciones por producto desde Fuentes activas.

El catálogo VitalHealth que viaja al prompt se construye desde product.template
(nombre, código, precio, categoría, descripción) y desde la regla
(valoracion.producto.regla — duración del paquete, cantidad mensual,
dosis sugerida, observaciones).

A partir de Fase 2.3, además, se busca en el texto extraído de las Fuentes
activas (valoracion.fuente.extracted_text) tres secciones tipo "Beneficio",
"Modo de empleo" y "Puede apoyar a personas con" por producto, y se inyectan
en el bloque del catálogo cuando se encuentran.

Heurística:
  1. Para cada producto, buscar el nombre del producto en el texto de las
     fuentes (case-insensitive, tolerante a espacios).
  2. Tomar una ventana desde la aparición del nombre hasta el inicio del
     siguiente producto (o ~2000 chars).
  3. En esa ventana buscar etiquetas (case-insensitive, multi-línea):
       Beneficio:        / Beneficios:
       Modo de empleo:   / Modo de uso: / Forma de empleo: / Posología:
       Puede apoyar a personas con:  / Apoya a personas con:
       / Indicado para personas con:
  4. El contenido de cada sección va desde el final de la etiqueta hasta
     la siguiente etiqueta (de cualquier sección) o hasta una "etiqueta
     terminadora" como Ingredientes, Presentación, Contraindicaciones,
     Composición, Advertencias, Recomendación.
  5. El contenido se compacta (whitespace) y se cap a 500 chars por sección.

Tolerante a:
  * mayúsculas/minúsculas en etiquetas
  * "Beneficio" o "Beneficios"
  * "Modo de empleo" / "Modo de uso" / "Forma de empleo" / "Posología"
  * Variantes con tilde en terminadores ("presentación", "composición")
  * Espacios extra, dos puntos / guion / em-dash como separador
"""
import re
import logging

_logger = logging.getLogger(__name__)


# Cap por sección extraída (chars) — evita inflar el bloque del catálogo
MAX_SECTION_CHARS = 500
# Ventana máxima por producto (chars) si no se encuentra el siguiente producto
DEFAULT_WINDOW_CHARS = 2500


# Mapeo (regex_etiqueta → clave del dict resultado)
_LABEL_MAP = (
    (r'beneficios?', 'beneficio'),
    (r'modo\s+de\s+(?:empleo|uso)', 'modo_empleo'),
    (r'forma\s+de\s+(?:empleo|uso)', 'modo_empleo'),
    (r'posolog[ií]a', 'modo_empleo'),
    (r'puede\s+apoyar\s+a\s+personas\s+con', 'apoya_a'),
    (r'apoya\s+a\s+personas\s+con', 'apoya_a'),
    (r'indicad[oa]\s+para\s+personas\s+con', 'apoya_a'),
)

# Etiquetas terminadoras: paran el contenido de cualquier sección extraíble
_TERMINATOR_PATTERNS = (
    r'ingredientes?',
    r'presentaci[oó]n',
    r'contraindicaciones?',
    r'composici[oó]n',
    r'advertencias?',
    r'recomendaci[oó]n',
    r'precauciones',
    r'almacenamiento',
)


_RE_LABEL_ANY = None  # construido lazy


def _build_label_regex():
    """Compila el regex combinado para encontrar cualquier etiqueta (incluyendo
    terminadores y separadores horizontales). Cachea el resultado.
    """
    global _RE_LABEL_ANY
    if _RE_LABEL_ANY is not None:
        return _RE_LABEL_ANY
    parts = [p for p, _ in _LABEL_MAP] + list(_TERMINATOR_PATTERNS)
    label_alt = '|'.join(parts)
    # Dos alternativas:
    #   A) Una etiqueta de las conocidas seguida de : - – —
    #   B) Una línea de separador (=== o --- o más) sola → corta contenido
    combined = (
        r'(?im)'
        r'(?:'
        r'^[ \t]*(' + label_alt + r')[ \t]*[:\-–—][ \t]*'
        r'|'
        r'^[ \t]*(=+|-{3,}|_{3,})[ \t]*$'
        r')'
    )
    _RE_LABEL_ANY = re.compile(combined)
    return _RE_LABEL_ANY


def _label_to_key(label_text):
    """Dada la etiqueta encontrada, devuelve la clave de salida o None
    (cuando es una etiqueta terminadora).
    """
    norm = label_text.strip().lower()
    for pat, key in _LABEL_MAP:
        if re.fullmatch(r'(?i)' + pat, norm):
            return key
    return None


def _extract_sections_from_window(window):
    """Extrae beneficio, modo_empleo, apoya_a de un trozo de texto.

    Returns dict con esas 3 claves, cada una str o None.
    """
    result = {'beneficio': None, 'modo_empleo': None, 'apoya_a': None}
    if not window:
        return result

    regex = _build_label_regex()
    matches = list(regex.finditer(window))
    if not matches:
        return result

    for i, m in enumerate(matches):
        label = m.group(1)
        if not label:
            # Es un separador horizontal — actúa como terminador puro,
            # no inicia sección.
            continue
        key = _label_to_key(label)
        if key is None:
            continue  # etiqueta terminadora, no es campo extraíble
        if result.get(key):
            continue  # primera aparición gana
        content_start = m.end()
        if i + 1 < len(matches):
            content_end = matches[i + 1].start()
        else:
            content_end = len(window)
        content = window[content_start:content_end].strip()
        # Compactar whitespace
        content = ' '.join(content.split())
        if not content:
            continue
        if len(content) > MAX_SECTION_CHARS:
            content = content[:MAX_SECTION_CHARS].rstrip() + '...'
        result[key] = content
    return result


def _find_product_anchor(name, text):
    """Busca la primera aparición del nombre del producto en `text`,
    case-insensitive y tolerante a espacios. Devuelve la posición (int)
    o None.

    Si el nombre tiene paréntesis o caracteres regex-especiales, se
    escapan; los espacios internos se relajan a \\s+ para que tolere
    saltos de línea o tabs en el PDF.
    """
    if not name or not text:
        return None
    tokens = re.split(r'\s+', name.strip())
    if not tokens:
        return None
    flexible = r'\s+'.join(re.escape(t) for t in tokens if t)
    if not flexible:
        return None
    try:
        pattern = re.compile(r'(?i)' + flexible)
    except re.error:
        return None
    m = pattern.search(text)
    return m.start() if m else None


def extract_product_sections_from_fuentes(fuentes_text, products):
    """Punto de entrada principal.

    Args:
        fuentes_text: str con el texto concatenado de las fuentes activas.
        products: iterable de product.template (o cualquier objeto con .id y .name).

    Returns:
        dict {product_id: {'beneficio': str|None,
                           'modo_empleo': str|None,
                           'apoya_a': str|None}}

    Si fuentes_text está vacío o no se encuentra ningún producto, devuelve {}.
    El llamador debe inyectar las secciones encontradas en el bloque del
    catálogo, y cuando no existan para un producto, mantener el bloque actual.
    """
    if not fuentes_text or not products:
        return {}

    # Paso 1: localizar anchors de cada producto y guardar la primera aparición
    anchors = []   # list of (start_pos, product)
    for prod in products:
        if not getattr(prod, 'name', None):
            continue
        pos = _find_product_anchor(prod.name, fuentes_text)
        if pos is None:
            continue
        anchors.append((pos, prod))

    if not anchors:
        return {}

    # Paso 2: ordenar por posición para definir ventanas no solapadas
    anchors.sort(key=lambda x: x[0])

    out = {}
    seen_ids = set()
    for i, (pos, prod) in enumerate(anchors):
        if prod.id in seen_ids:
            continue
        seen_ids.add(prod.id)
        # ventana: hasta el siguiente producto o hasta DEFAULT_WINDOW_CHARS
        if i + 1 < len(anchors):
            next_pos = anchors[i + 1][0]
        else:
            next_pos = len(fuentes_text)
        window_end = min(next_pos, pos + DEFAULT_WINDOW_CHARS)
        window = fuentes_text[pos:window_end]
        sections = _extract_sections_from_window(window)
        if any(sections.values()):
            out[prod.id] = sections

    _logger.debug(
        "catalog_enricher: %d producto(s) enriquecido(s) desde %d chars de fuentes",
        len(out), len(fuentes_text),
    )
    return out

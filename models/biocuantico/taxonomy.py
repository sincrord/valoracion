# -*- coding: utf-8 -*-
"""Motor BioCuántico v3 — Taxonomía declarativa centralizada (Fase 3.1).

Este módulo es el ÚNICO lugar donde se definen los sistemas corporales,
sus patrones de reconocimiento, anti-patrones y "contextual pulls"
(palabras de contexto cercano que ayudan a desambiguar).

Antes de esta fase, las reglas vivían en 4 lugares (seed XML,
_SISTEMAS_FALLBACK, CRITICAL_PARAM_OVERRIDES, AMBIGUOUS_PATTERNS).
Agregar un parámetro nuevo requería tocar 3 archivos. Ahora basta con
agregar UNA entrada aquí.

IMPORTANTE:
  * No depende de Odoo. Es un módulo puro de Python.
  * No depende de ningún cliente o caso real. Está pensado en términos
    clínicos generales (fisiología, no productos comerciales).
  * No lee ni escribe BD.

Modelo de clasificación:
  Cada sistema acumula un score sumando los `patterns` que matchean y
  restando los `anti_patterns`. Si el parámetro contiene un disambiguator
  (lista compartida entre sistemas, ej. "complacencia"), el sistema con
  más `contextual_pulls` en el texto cercano de la tabla gana el desempate.

  El classifier.py de Fase 3.3 consume este catálogo. taxonomy.py NO
  decide clasificación — sólo declara las reglas.
"""
import re
import unicodedata


# =====================================================================
# Estructuras declarativas (immutables — tuplas y frozensets cuando aplica)
# =====================================================================

class Pattern(object):
    """Un patrón a buscar en el texto normalizado de un parámetro o fila.

    Args:
        needle: str (substring exacto) o re.Pattern (regex pre-compilado).
        score: peso entero. Cuanto más alto, más confianza aporta este
            patrón a la clasificación del sistema.
        match_mode:
            * 'substring' (default): needle in text
            * 'whole_word': needle como palabra completa (boundary)
            * 'exact': text == needle (igualdad estricta)
            * 'regex': needle es re.Pattern
        notes: comentario opcional para auditoría.
    """
    __slots__ = ('needle', 'score', 'match_mode', 'notes', '_re')

    def __init__(self, needle, score=5, match_mode='substring', notes=''):
        self.needle = needle
        self.score = int(score)
        self.match_mode = match_mode
        self.notes = notes
        # Pre-compilar regex si es 'whole_word' (frecuente)
        if match_mode == 'whole_word' and isinstance(needle, str):
            self._re = re.compile(r'(?:^|\W)' + re.escape(needle) + r'(?:$|\W)')
        elif match_mode == 'regex':
            if isinstance(needle, str):
                self._re = re.compile(needle)
            else:
                self._re = needle
        else:
            self._re = None

    def matches(self, text):
        """True si este patrón se encuentra en `text` (ya normalizado).

        El llamador es responsable de normalizar el texto antes (lower,
        sin acentos, whitespace colapsado). Ver extractor.normalize_text.
        """
        if not text or not self.needle:
            return False
        if self.match_mode == 'substring':
            return self.needle in text
        if self.match_mode == 'whole_word':
            return self._re.search(text) is not None
        if self.match_mode == 'exact':
            return text == self.needle
        if self.match_mode == 'regex':
            return self._re.search(text) is not None
        return False

    def __repr__(self):
        return "Pattern(%r, score=%d, mode=%s)" % (
            self.needle, self.score, self.match_mode,
        )


class System(object):
    """Definición declarativa de un sistema corporal.

    Args:
        code: identificador interno único (ej. 'cardiovascular').
        label: nombre legible para audit/output.
        patterns: lista de Pattern que CONFIRMAN este sistema.
        anti_patterns: lista de Pattern que penalizan (matchean → resta).
        contextual_pulls: lista de strings — palabras de contexto cercano
            (en la misma tabla/sección) que aumentan score si están.
        min_score_to_assign: umbral mínimo para que classifier asigne
            este sistema al parámetro (evita asignaciones ruidosas).
        catch_all: si True, este sistema actúa como destino último cuando
            ningún otro alcanza umbral. Sólo uno debe ser catch_all=True.
    """
    __slots__ = ('code', 'label', 'patterns', 'anti_patterns',
                 'contextual_pulls', 'min_score_to_assign', 'catch_all')

    def __init__(self, code, label, patterns=None, anti_patterns=None,
                 contextual_pulls=None, min_score_to_assign=5,
                 catch_all=False):
        self.code = code
        self.label = label
        self.patterns = list(patterns or [])
        self.anti_patterns = list(anti_patterns or [])
        self.contextual_pulls = tuple(contextual_pulls or [])
        self.min_score_to_assign = int(min_score_to_assign)
        self.catch_all = bool(catch_all)

    def __repr__(self):
        return "System(%s, %d patterns)" % (self.code, len(self.patterns))


# =====================================================================
# Helpers de normalización (compartidos con extractor.py)
# =====================================================================
# Caracteres invisibles a eliminar antes de cualquier matching.
INVISIBLE_CHARS = (
    '​',  # zero width space
    '‌',  # ZWNJ
    '‍',  # ZWJ
    '⁠',  # word joiner
    '﻿',  # BOM
    '­',  # soft hyphen
)


def normalize_text(text):
    """Normalización fuerte: lower + sin acentos + sin invisibles + NBSP→
    espacio + whitespace colapsado. Idempotente.

    Esta función es la base. Tanto extractor como classifier la usan.
    Su contrato es estable y se test-ea en aislado.
    """
    if not text:
        return ''
    t = str(text)
    for inv in INVISIBLE_CHARS:
        if inv in t:
            t = t.replace(inv, '')
    t = t.replace(' ', ' ').lower()
    nfkd = unicodedata.normalize('NFKD', t)
    cleaned = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return ' '.join(cleaned.split())


# =====================================================================
# 14 SISTEMAS CORPORALES — definición declarativa
# =====================================================================
# Las listas de patterns están organizadas por:
#   - patrones largos (12+ chars) con score alto: 10
#   - patrones medianos (6-12 chars) con score: 7
#   - patrones cortos (3-5 chars) con score: 5
#   - patrones genéricos compartidos: 3
# Esto evita que "ojo" (3 chars) gane sobre "cardiovascular" (14 chars)
# accidentalmente — sin necesidad de hardcodear overrides por nombre.

SYSTEMS = [
    # --- Cardiovascular ---
    System(
        code='cardiovascular',
        label='Sistema Cardiovascular',
        patterns=[
            Pattern('cardiovascular', score=10),
            Pattern('demanda de sangre', score=10),
            Pattern('demanda miocardial', score=10),
            Pattern('cerebrovascular', score=10),
            Pattern('arteriosclerosis', score=10),
            Pattern('aterosclerosis', score=10),
            Pattern('ateromatosis', score=10),
            Pattern('oxigenacion sanguinea', score=10),
            Pattern('oxigeno en la sangre', score=10),
            Pattern('miocardial', score=8),
            Pattern('miocardio', score=8),
            Pattern('presion arterial', score=8),
            Pattern('ritmo cardiaco', score=8),
            Pattern('hemoglobina', score=7, notes='también puede ser metabólico (glicada)'),
            Pattern('hematocrito', score=7),
            Pattern('plaquetas', score=7),
            Pattern('arteria', score=6),
            Pattern('arterial', score=6),
            Pattern('aterom', score=6),
            Pattern('infarto', score=6),
            Pattern('angina', score=6),
            Pattern('taquicardia', score=6),
            Pattern('bradicardia', score=6),
            Pattern('arritmia', score=6),
            Pattern('circulacion', score=5),
            Pattern('corazon', score=5),
            Pattern('vascular', score=4, notes='ambiguo — puede combinar'),
            Pattern('sangre', score=3, notes='muy genérico, peso bajo'),
        ],
        anti_patterns=[
            Pattern('pulmonar', score=3),  # presión pulmonar no es CV
        ],
        contextual_pulls=['vascular', 'arteria', 'circulacion', 'corazon',
                          'aortico', 'aorta', 'hemodinamic'],
    ),

    # --- Digestivo / hepático ---
    System(
        code='digestivo',
        label='Sistema Digestivo',
        patterns=[
            Pattern('gastrointestinal', score=10),
            Pattern('helicobacter pylori', score=10),
            Pattern('reflujo gastroesofagico', score=10),
            Pattern('flora intestinal', score=8),
            Pattern('microbiota', score=8),
            Pattern('acidez gastrica', score=8),
            Pattern('ulcera gastrica', score=8),
            Pattern('estreñimiento', score=7),
            Pattern('estrenimiento', score=7),  # sin tilde
            Pattern('helicobacter', score=8),
            Pattern('digestivo', score=7),
            Pattern('digestion', score=6),
            Pattern('higado', score=7),
            Pattern('hepatico', score=7),
            Pattern('hepatic', score=6),
            Pattern('vesicula', score=7),
            Pattern('biliar', score=6),
            Pattern('bilis', score=6),
            Pattern('intestino', score=6),
            Pattern('estomago', score=6),
            Pattern('colon', score=5),
            Pattern('duodeno', score=5),
            Pattern('gastritis', score=6),
            Pattern('pancreas exocrino', score=8),
        ],
        contextual_pulls=['digestion', 'intestin', 'higado', 'biliar'],
    ),

    # --- Endocrino / hormonal ---
    System(
        code='endocrino',
        label='Sistema Endocrino',
        patterns=[
            Pattern('hormona de crecimiento', score=10),
            Pattern('tiroglobulina', score=10),
            Pattern('triyodotironina', score=10),
            Pattern('hipotiroidismo', score=10),
            Pattern('hipertiroidismo', score=10),
            Pattern('tiroideo', score=8),
            Pattern('tiroides', score=8),
            Pattern('tiroidea', score=8),
            Pattern('tiroxina', score=8),
            Pattern('suprarrenal', score=8),
            Pattern('hipofisis', score=8),
            Pattern('hipofisiario', score=8),
            Pattern('progesterona', score=8),
            Pattern('prolactina', score=8),
            Pattern('testosterona', score=8),
            Pattern('estradiol', score=8),
            Pattern('estrogeno', score=8),
            Pattern('cortisol', score=8),
            Pattern('aldosterona', score=8),
            Pattern('melatonina', score=8),
            Pattern('insulina', score=8),
            Pattern('oxitocina', score=8),
            Pattern('hormonal', score=6),
            Pattern('glandula', score=5),
            Pattern('endocrino', score=7),
            # Hormonas siglas — whole-word para evitar matches espurios
            Pattern('tsh', score=8, match_mode='whole_word'),
            Pattern('t3', score=6, match_mode='whole_word'),
            Pattern('t4', score=6, match_mode='whole_word'),
            Pattern('fsh', score=6, match_mode='whole_word'),
            Pattern('lh', score=5, match_mode='whole_word'),
            Pattern('dhea', score=7, match_mode='whole_word'),
        ],
        contextual_pulls=['hormonal', 'glandula', 'tiroidea'],
    ),

    # --- Metabólico / nutricional ---
    System(
        code='metabolico',
        label='Sistema Metabólico / Nutricional',
        patterns=[
            Pattern('hemoglobina glicada', score=10),
            Pattern('perfil lipidico', score=10),
            Pattern('balance nutricional', score=10),
            Pattern('sintesis proteica', score=10),
            Pattern('indice metabolico', score=10),
            Pattern('grasa corporal', score=8),
            Pattern('masa muscular', score=8),
            Pattern('masa magra', score=8),
            Pattern('aminoacidos', score=8),
            Pattern('aminoacido', score=7),
            Pattern('proteinas', score=7),
            Pattern('proteina', score=6),
            Pattern('carbohidratos', score=7),
            Pattern('carbohidrato', score=6),
            Pattern('lipidos', score=7),
            Pattern('lipido', score=6),
            Pattern('lipidico', score=6),
            Pattern('lipoproteina', score=7),
            Pattern('triglicerido', score=8),
            Pattern('trigliceridos', score=8),
            Pattern('colesterol total', score=10),
            Pattern('colesterol', score=8),
            Pattern('glucosa', score=8),
            Pattern('glucemia', score=8),
            Pattern('glicada', score=7),
            Pattern('metabolico', score=7),
            Pattern('metabolismo', score=7),
            Pattern('nutricional', score=7),
            Pattern('vitamina', score=6),
            Pattern('mineral nutricional', score=8),
            Pattern('acido folico', score=8),
            Pattern('omega 3', score=8),
            Pattern('omega 6', score=8),
            Pattern('hdl', score=8, match_mode='whole_word'),
            Pattern('ldl', score=8, match_mode='whole_word'),
            Pattern('vldl', score=8, match_mode='whole_word'),
            Pattern('hba1c', score=10, match_mode='whole_word'),
            Pattern('b12', score=6, match_mode='whole_word'),
        ],
        anti_patterns=[
            # Si el parámetro tiene contexto cardiovascular fuerte, restar
            Pattern('miocardial', score=8),
            Pattern('cerebrovascular', score=8),
        ],
        contextual_pulls=['nutricion', 'lipidico', 'metabolic', 'glicemia'],
    ),

    # --- Inmune / inflamatorio ---
    System(
        code='inmune',
        label='Sistema Inmune',
        patterns=[
            Pattern('iga secretora', score=10),
            Pattern('inmunologico', score=10),
            Pattern('autoinmune', score=10),
            Pattern('inflamacion', score=8),
            Pattern('inflamatori', score=7),
            Pattern('linfocitos', score=8),
            Pattern('leucocitos', score=8),
            Pattern('neutrofilos', score=8),
            Pattern('defensas', score=7),
            Pattern('inmune', score=7),
            Pattern('alergi', score=6, notes='también puede ser restricción'),
            Pattern('igg', score=8, match_mode='whole_word'),
            Pattern('igm', score=8, match_mode='whole_word'),
            Pattern('ige', score=8, match_mode='whole_word'),
        ],
        contextual_pulls=['defensas', 'inflama'],
    ),

    # --- Linfático ---
    System(
        code='linfatico',
        label='Sistema Linfático',
        patterns=[
            Pattern('drenaje linfatico', score=10),
            Pattern('linfatico', score=8),
            Pattern('ganglios', score=7),
            Pattern('linfa', score=6),
            Pattern('bazo', score=6),
        ],
        contextual_pulls=['linfa'],
    ),

    # --- Nervioso / cognitivo ---
    System(
        code='nervioso',
        label='Sistema Nervioso',
        patterns=[
            Pattern('sistema nervioso autonomo', score=10),
            Pattern('equilibrio autonomico', score=10),
            Pattern('neurotransmisor', score=10),
            Pattern('neurologico', score=8),
            Pattern('serotonina', score=8),
            Pattern('dopamina', score=8),
            Pattern('acetilcolina', score=8),
            Pattern('noradrenalina', score=8),
            Pattern('neuronal', score=7),
            Pattern('sinapsis', score=7),
            Pattern('nervioso', score=7),
            Pattern('cerebro', score=6),
            Pattern('nervios', score=6),
            Pattern('gaba', score=6, match_mode='whole_word'),
            Pattern('snc', score=6, match_mode='whole_word'),
            # Síntomas cognitivos
            Pattern('ansiedad', score=8),
            Pattern('insomnio', score=8),
            Pattern('depresion', score=7),
            Pattern('estres', score=6),
            Pattern('memoria', score=6),
            Pattern('concentracion', score=6),
            Pattern('fatiga mental', score=8),
        ],
        contextual_pulls=['cognitiv', 'sueño', 'sueno'],
    ),

    # --- Respiratorio ---
    System(
        code='respiratorio',
        label='Sistema Respiratorio',
        patterns=[
            Pattern('capacidad residual funcional', score=10),
            Pattern('vias respiratorias', score=10),
            Pattern('capacidad pulmonar', score=10),
            Pattern('capacidad vital forzada', score=10),
            Pattern('capacidad vital', score=10),
            Pattern('capacidad residual', score=10),
            Pattern('volumen tidal', score=10),
            Pattern('volumen residual', score=10),
            Pattern('espirometr', score=8),
            Pattern('respiratorio', score=8),
            Pattern('pulmonar', score=8),
            Pattern('pulmones', score=8),
            Pattern('bronquios', score=7),
            Pattern('alveolo', score=7),
            Pattern('alveolar', score=7),
            Pattern('diafragma', score=6),
            Pattern('frc', score=7, match_mode='whole_word'),
            Pattern('fev1', score=8, match_mode='whole_word'),
        ],
        contextual_pulls=['pulmonar', 'bronquio', 'alveol', 'respiratori'],
    ),

    # --- Renal / urinario ---
    System(
        code='urinario',
        label='Sistema Urinario',
        patterns=[
            Pattern('filtrado glomerular', score=10),
            Pattern('acido urico', score=8),
            Pattern('creatinina', score=8),
            Pattern('urinario', score=7),
            Pattern('renal', score=7),
            Pattern('riñon', score=7),
            Pattern('riñones', score=7),
            Pattern('rinon', score=6),  # sin tilde
            Pattern('vejiga', score=7),
            Pattern('urea', score=6),
            Pattern('nefron', score=6),
        ],
        contextual_pulls=['renal', 'urinari'],
    ),

    # --- Reproductor / ginecológico ---
    System(
        code='reproductor',
        label='Sistema Reproductor',
        patterns=[
            Pattern('ginecologico', score=10),
            Pattern('endometrio', score=8),
            Pattern('menstrual', score=7),
            Pattern('reproductor', score=7),
            Pattern('prostata', score=7),
            Pattern('ovarios', score=7),
            Pattern('testiculos', score=7),
            Pattern('utero', score=6),
            Pattern('fertilidad', score=6),
            Pattern('libido', score=6),
            Pattern('menopausia', score=8),
        ],
        contextual_pulls=['hormonal', 'menstr'],
    ),

    # --- Musculoesquelético ---
    System(
        code='musculoesqueletico',
        label='Sistema Musculoesquelético',
        patterns=[
            Pattern('musculoesqueletico', score=10),
            Pattern('densidad osea', score=10),
            Pattern('colageno tipo i', score=10),
            Pattern('osteoporosis', score=10),
            Pattern('osteopenia', score=10),
            Pattern('articulaciones', score=8),
            Pattern('articulacion', score=7),
            Pattern('tendinitis', score=8),
            Pattern('tendon', score=6),
            Pattern('lumbar', score=7),
            Pattern('cervical', score=7),
            Pattern('dorsal', score=6),
            Pattern('sacro', score=5),
            Pattern('rodilla', score=6),
            Pattern('cadera', score=6),
            Pattern('hombro', score=6),
            Pattern('huesos', score=7),
            Pattern('hueso', score=6),
            Pattern('oseo', score=6),
            Pattern('columna', score=6),
            Pattern('calcio oseo', score=8),
        ],
        contextual_pulls=['articula', 'hueso', 'oseo', 'lumbar', 'columna'],
    ),

    # --- Sensorial (visión / audición / etc.) ---
    System(
        code='sensorial',
        label='Sistema Sensorial',
        patterns=[
            Pattern('actividad celular del ojo', score=10),
            Pattern('agudeza visual', score=10),
            Pattern('nervio optico', score=10),
            Pattern('celulas oculares', score=10),
            Pattern('celular del ojo', score=10),
            Pattern('audicion alterada', score=10),
            Pattern('fatiga visual', score=10),
            Pattern('vision', score=7),
            Pattern('visual', score=6),
            Pattern('ocular', score=7),
            Pattern('retina', score=8),
            Pattern('retiniano', score=8),
            Pattern('cornea', score=7),
            Pattern('cristalino', score=8),
            Pattern('macula', score=7),
            Pattern('coclea', score=7),
            Pattern('auditivo', score=6),
            Pattern('audicion', score=6),
            Pattern('lacrimal', score=6),
            Pattern('olfato', score=6),
            Pattern('gusto', score=5),
            Pattern('equilibrio', score=5, notes='solo: vestibular'),
            Pattern('sensorial', score=6),
            # Cortos — whole_word para evitar matches espurios
            Pattern('ojo', score=7, match_mode='whole_word'),
            Pattern('ojos', score=7, match_mode='whole_word'),
            Pattern('oido', score=7, match_mode='whole_word'),
            Pattern('iris', score=5, match_mode='whole_word'),
            Pattern('pupila', score=5),
        ],
        contextual_pulls=['vision', 'auditiv', 'ocular'],
    ),

    # --- Tegumentario (piel/cabello/uñas) ---
    System(
        code='tegumentario',
        label='Sistema Tegumentario',
        patterns=[
            Pattern('humectacion cutanea', score=10),
            Pattern('hidratacion cutanea', score=10),
            Pattern('elasticidad cutanea', score=10),
            Pattern('manchas cutaneas', score=8),
            Pattern('bolsas en ojos', score=10),
            Pattern('tegumentario', score=8),
            Pattern('dermatologico', score=8),
            Pattern('epidermis', score=7),
            Pattern('dermis', score=7),
            Pattern('celulitis', score=7),
            Pattern('colageno', score=7),
            Pattern('arrugas', score=7),
            Pattern('arruga', score=6),
            Pattern('cabello', score=6),
            Pattern('piel', score=6),
            Pattern('flacidez', score=6),
            Pattern('estria', score=6),
            Pattern('sebaceo', score=6),
            Pattern('sebo', score=5),
        ],
        contextual_pulls=['piel', 'cuta', 'derm'],
    ),

    # --- Toxicidad / metales pesados ---
    System(
        code='toxicidad',
        label='Toxicidad / Metales pesados',
        patterns=[
            Pattern('metales pesados', score=10),
            Pattern('exposicion ambiental', score=10),
            Pattern('biotransformacion', score=10),
            Pattern('xenobioticos', score=10),
            Pattern('desintoxicacion', score=10),
            Pattern('detox hepatico', score=10),
            Pattern('detox', score=8, notes='standalone, captura "detox" sin sufijo'),
            Pattern('carga toxica', score=10),
            Pattern('intoxicacion', score=10),
            Pattern('contaminantes', score=8),
            Pattern('metal pesado', score=10),
            Pattern('toxicidad', score=8),
            Pattern('toxico', score=6),
            # Metales específicos
            Pattern('aluminio', score=10),
            Pattern('arsenico', score=10),
            Pattern('mercurio', score=10),
            Pattern('plomo', score=10),
            Pattern('cadmio', score=10),
            Pattern('antimonio', score=10),
            Pattern('berilio', score=10),
            Pattern('talio', score=10),
            Pattern('niquel', score=8),
            Pattern('manganeso', score=7),
            Pattern('cromo', score=6),
            Pattern('bario', score=8),
            Pattern('cobre', score=4, notes='cobre puede ser nutricional'),
            Pattern('zinc', score=4, notes='zinc puede ser nutricional'),
        ],
        contextual_pulls=['detox', 'desintox', 'metal'],
    ),

    # --- Catch-all ---
    System(
        code='otros',
        label='Otros / sin clasificar',
        patterns=[],  # nada matchea explícitamente
        catch_all=True,
        min_score_to_assign=0,
    ),
]


# =====================================================================
# Patrones de RUIDO NO-CLÍNICO
# =====================================================================
# Estos parámetros NO son hallazgos clínicos — son texto de layout
# (figuras, tablas, ilustraciones, cabeceras, notas) que pdfplumber a
# veces extrae como filas con valores numéricos espurios.
# El classifier.py los desvía a parametros_descartados antes del ranking.

NOISE_PATTERNS = (
    Pattern('ilustracion'),
    Pattern('ilustraciones'),
    Pattern('imagen'),
    Pattern('imagenes'),
    Pattern('figura'),
    Pattern('figuras'),
    Pattern('grafico'),
    Pattern('graficos'),
    Pattern('grafica'),
    Pattern('tabla siguiente'),
    Pattern('tabla anterior'),
    Pattern('tabla resumen'),
    Pattern('pag.'),
    Pattern('pagina '),
    Pattern('logo'),
    Pattern('logotipo'),
    Pattern('nota:'),
    Pattern('nota al pie'),
    Pattern('pie de pagina'),
    Pattern('ejemplo'),
    Pattern('ejemplos'),
    Pattern('explicacion'),
    Pattern('introduccion'),
    Pattern('introductorio'),
    Pattern('descripcion del grafico'),
    Pattern('descripcion de la tabla'),
    Pattern('titulo'),
    Pattern('subtitulo'),
    Pattern('encabezado'),
    Pattern('header'),
    Pattern('footer'),
    Pattern('anexo'),
    Pattern('apendice'),
    Pattern('glosario'),
    Pattern('bibliografia'),
    Pattern('referencias bibliograficas'),
    Pattern('indice general'),
    Pattern('tabla de contenido'),
    Pattern('contenido del informe'),
)


# =====================================================================
# Patrones de BAJA RELEVANCIA clínica
# =====================================================================
# El parámetro se mantiene en su sistema pero se BAJA un nivel de
# severidad (severo → moderado, etc.) para que no inflame las prioridades.

LOW_RELEVANCE_PATTERNS = (
    Pattern('arrugas profundas'),
    Pattern('manchas cutaneas'),
    Pattern('celulitis'),
    Pattern('arruga'),
    Pattern('cosmetic'),
    Pattern('estetic'),
    Pattern('apariencia'),
    Pattern('olor corporal'),
    Pattern('sudoracion menor'),
)


# =====================================================================
# Auditoría / introspección del catálogo
# =====================================================================
def all_systems():
    """Devuelve la lista inmutable de sistemas."""
    return tuple(SYSTEMS)


def get_system(code):
    """Devuelve un System por code, o None."""
    for s in SYSTEMS:
        if s.code == code:
            return s
    return None


def all_codes():
    """Lista de codes de sistemas (incluye 'otros' catch-all)."""
    return tuple(s.code for s in SYSTEMS)


def validate_catalog():
    """Validación interna del catálogo. Devuelve lista de problemas (vacía
    si todo OK). Útil como test de regresión: si alguien añade un patrón
    mal escrito o duplicado, este test lo detecta.
    """
    problems = []
    # 1. Códigos únicos
    codes = [s.code for s in SYSTEMS]
    if len(codes) != len(set(codes)):
        problems.append("Codes duplicados en SYSTEMS: %r" % codes)
    # 2. Sólo un catch_all
    catch_alls = [s.code for s in SYSTEMS if s.catch_all]
    if len(catch_alls) != 1:
        problems.append("Debe haber exactamente 1 catch_all; hay %d: %r"
                        % (len(catch_alls), catch_alls))
    # 3. Cada patrón debe ser Pattern (no string suelto)
    for s in SYSTEMS:
        for p in s.patterns + s.anti_patterns:
            if not isinstance(p, Pattern):
                problems.append("System %s: patrón no-Pattern: %r" % (s.code, p))
    # 4. Patrones con needle vacía
    for s in SYSTEMS:
        for p in s.patterns:
            if not p.needle:
                problems.append("System %s: patrón con needle vacía" % s.code)
    return problems

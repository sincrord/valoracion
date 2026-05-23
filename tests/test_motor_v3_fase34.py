# -*- coding: utf-8 -*-
"""Tests Motor BioCuántico v3 — Fase 3.4 (ranker puro).

REGLAS ARQUITECTÓNICAS:
    El PDF clasifica → classifier.py (Fase 3.2)
    El PDF tiene severidad → severity.py (Fase 3.3)
    El antecedente PRIORIZA → ranker.py (Fase 3.4) ← este módulo
    La IA interpreta → no se toca

Los tests validan que ranker.py:
  * convierte rows con severity en prioridades funcionales,
  * SOLO usa antecedente para boost de ranking (NUNCA reclasifica),
  * aplica penalización cuando leves dominan (ruido),
  * aplica boost multiplicativo a sistemas indicados por antecedente,
  * produce orden determinístico (score, severidad, hallazgos, label),
  * tope max_priorities,
  * pasa los 6 casos A-F end-to-end con classifier + severity + ranker,
  * NO está conectado al flujo productivo.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.valoracion.models.biocuantico.extractor import (
    extract_from_table, make_raw_row,
)
from odoo.addons.valoracion.models.biocuantico.classifier import classify
from odoo.addons.valoracion.models.biocuantico.severity import assign_severity
from odoo.addons.valoracion.models.biocuantico.ranker import (
    rank,
    compute_antecedente_boost,
    aggregate_by_system,
    score_system,
    WEIGHTS,
    LEVES_DOMINANT_PENALTY,
    ANTECEDENTE_BOOST_FACTOR,
    DEFAULT_MAX_PRIORITIES,
    ANTECEDENTE_TRIGGERS,
)


# Tablas multicaso (reutilizadas de Fase 3.3)
CASE_TABLES = {
    'A': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Digestivo', '', ''],
        ['Helicobacter pylori', '1.8', '0 - 1'],
        ['Acidez gástrica', '8.2', '4 - 7'],
        ['Tránsito intestinal', '0.3', '0.5 - 1.5'],
        ['Función hepática', '2.5', '1.0 - 1.5'],
        ['Bilis estancada', 'sí', 'no'],
        ['Sistema Toxicidad', '', ''],
        ['Aluminio', '85', '0 - 10'],
        ['Mercurio', '0.5', '0 - 0.1'],
        ['Detox hepático', 'bajo', 'normal'],
    ],
    'B': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Metabólico', '', ''],
        ['Glucosa basal', '110', '70 - 100'],
        ['HbA1c', '6.5', '4 - 5.6'],
        ['HDL', '25', '40 - 60'],
        ['LDL', '180', '0 - 100'],
        ['Triglicéridos', '210', '0 - 150'],
        ['Colesterol total', '250', '0 - 200'],
        ['Grasa corporal', '38', '15 - 25'],
        ['Insulina', '20', '5 - 15'],
    ],
    'C': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Endocrino', '', ''],
        ['TSH', '6.5', '0.5 - 4.5'],
        ['T4 libre', '0.5', '0.8 - 1.8'],
        ['T3 reverso', '350', '90 - 280'],
        ['Tiroglobulina', '120', '0 - 55'],
        ['Cortisol matutino', '32', '5 - 25'],
        ['Progesterona', '0.5', '5 - 30'],
        ['Estradiol', '180', '50 - 150'],
        ['Prolactina', '35', '5 - 25'],
        ['DHEA', '120', '50 - 270'],
    ],
    'D': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Musculoesquelético', '', ''],
        ['Densidad ósea lumbar', '0.7', '0.9 - 1.2'],
        ['Densidad ósea cadera', '0.75', '0.9 - 1.2'],
        ['Colágeno tipo I', '1.5', '2.5 - 4.0'],
        ['Articulación rodilla', '0.4', '0.8 - 1.0'],
        ['Sistema Inmune', '', ''],
        ['Inflamación articular', '2.5', '0 - 1'],
        ['Proteína C reactiva', '8.5', '0 - 5'],
        ['IgG', '1700', '700 - 1500'],
    ],
    'E': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Sistema Nervioso', '', ''],
        ['Serotonina', '80', '120 - 200'],
        ['Dopamina', '45', '60 - 100'],
        ['GABA', '0.8', '1.5 - 3.0'],
        ['Equilibrio autonómico', '0.4', '0.8 - 1.2'],
        ['Ansiedad subjetiva', '7', '0 - 3'],
        ['Insomnio nocturno', '5', '0 - 2'],
        ['Memoria a corto plazo', '6', '8 - 10'],
        ['Fatiga mental', '8', '0 - 3'],
    ],
    'F': [
        ['Parámetro', 'Valor', 'Rango'],
        ['Helicobacter', '1.5', '0 - 1'],
        ['HDL', '25', '40 - 60'],
        ['TSH', '5.5', '0.5 - 4.5'],
        ['Densidad ósea', '0.8', '0.9 - 1.2'],
        ['Serotonina', '70', '120 - 200'],
        ['Aluminio', '50', '0 - 10'],
        ['Cápsula renal', '0.7', '0.8 - 1.2'],
        ['Capacidad residual funcional', '1.8', '2.0 - 3.5'],
    ],
}


def _pipeline(case_key):
    """classifier + severity → rows listos para el ranker."""
    rows = extract_from_table(CASE_TABLES[case_key])
    classified = classify(rows)['kept']
    return assign_severity(classified)


def _mk(code, level, score=None, label=None, parametro='X'):
    """Atajo para crear una row sintética para tests unitarios del ranker."""
    score_map = {
        'critico': 5, 'severo': 4, 'moderado': 3, 'leve': 2,
        'normal': 1, 'optimo': 0, 'desconocida': 9,
    }
    return {
        'sistema_code': code,
        'sistema_label': label or code,
        'parametro_raw': parametro,
        'severity_level': level,
        'severity_score': score if score is not None else score_map[level],
    }


# =====================================================================
# 1) compute_antecedente_boost — mapeo antecedente → sistemas
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestComputeAntecedenteBoost(TransactionCase):

    def test_vacio_no_boost(self):
        self.assertEqual(compute_antecedente_boost(''), [])
        self.assertEqual(compute_antecedente_boost(None), [])

    def test_tiroides_boost_endocrino(self):
        codes = compute_antecedente_boost('Hipotiroidismo desde 2020')
        self.assertIn('endocrino', codes)

    def test_diabetes_boost_metabolico_endocrino(self):
        codes = compute_antecedente_boost('Diabetes tipo 2 controlada')
        self.assertIn('metabolico', codes)
        self.assertIn('endocrino', codes)

    def test_lumbalgia_boost_musculo(self):
        codes = compute_antecedente_boost('Lumbalgia crónica')
        self.assertIn('musculoesqueletico', codes)

    def test_detox_boost_toxicidad_digestivo(self):
        codes = compute_antecedente_boost('Quiere desintoxicación de metales')
        self.assertIn('toxicidad', codes)
        self.assertIn('digestivo', codes)

    def test_estres_ansiedad_boost_nervioso(self):
        c1 = compute_antecedente_boost('Estrés crónico')
        c2 = compute_antecedente_boost('Ansiedad e insomnio')
        self.assertIn('nervioso', c1)
        self.assertIn('nervioso', c2)

    def test_acentos_y_mayusculas_normalizados(self):
        """'HÍGADO' debe encontrar el trigger 'higado'."""
        codes = compute_antecedente_boost('Problemas de HÍGADO')
        self.assertIn('digestivo', codes)

    def test_sin_trigger_devuelve_vacio(self):
        self.assertEqual(
            compute_antecedente_boost('texto totalmente irrelevante xyz'),
            [],
        )

    def test_es_funcion_pura(self):
        """Llamarla dos veces con el mismo input debe devolver lo mismo."""
        a = compute_antecedente_boost('Hipertensión arterial')
        b = compute_antecedente_boost('Hipertensión arterial')
        self.assertEqual(a, b)
        self.assertIn('cardiovascular', a)


# =====================================================================
# 2) aggregate_by_system — agrupa hallazgos y cuenta stats
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestAggregateBySystem(TransactionCase):

    def test_agrupacion_basica(self):
        rows = [
            _mk('digestivo', 'severo'),
            _mk('digestivo', 'moderado'),
            _mk('digestivo', 'leve'),
            _mk('endocrino', 'severo'),
        ]
        agg = aggregate_by_system(rows)
        self.assertEqual(agg['digestivo']['severos'], 1)
        self.assertEqual(agg['digestivo']['moderados'], 1)
        self.assertEqual(agg['digestivo']['leves'], 1)
        self.assertEqual(agg['endocrino']['severos'], 1)

    def test_critico_cuenta_como_severo(self):
        rows = [_mk('cardiovascular', 'critico')]
        agg = aggregate_by_system(rows)
        self.assertEqual(agg['cardiovascular']['severos'], 1)

    def test_normales_y_desconocidos_no_cuentan(self):
        rows = [
            _mk('digestivo', 'normal'),
            _mk('digestivo', 'desconocida'),
            _mk('digestivo', 'optimo'),
        ]
        agg = aggregate_by_system(rows)
        self.assertEqual(agg['digestivo']['severos'], 0)
        self.assertEqual(agg['digestivo']['moderados'], 0)
        self.assertEqual(agg['digestivo']['leves'], 0)

    def test_otros_se_descarta(self):
        """La categoría 'otros' (catch-all) NO debe entrar al ranking."""
        rows = [_mk('otros', 'severo'), _mk('digestivo', 'leve')]
        agg = aggregate_by_system(rows)
        self.assertNotIn('otros', agg)
        self.assertIn('digestivo', agg)

    def test_hallazgos_ordenados_por_severidad_desc(self):
        rows = [
            _mk('digestivo', 'leve', parametro='leve_1'),
            _mk('digestivo', 'severo', parametro='sev_1'),
            _mk('digestivo', 'moderado', parametro='mod_1'),
        ]
        agg = aggregate_by_system(rows)
        halls = agg['digestivo']['hallazgos']
        self.assertEqual(halls[0]['parametro_raw'], 'sev_1')
        self.assertEqual(halls[-1]['parametro_raw'], 'leve_1')

    def test_max_severity_se_propaga(self):
        rows = [
            _mk('digestivo', 'leve'),
            _mk('digestivo', 'severo'),
        ]
        agg = aggregate_by_system(rows)
        self.assertEqual(agg['digestivo']['sev_max_level'], 'severo')
        self.assertEqual(agg['digestivo']['sev_max_score'], 4)

    def test_rows_sin_sistema_se_ignoran(self):
        rows = [{'sistema_code': None, 'severity_level': 'severo'},
                _mk('digestivo', 'leve')]
        agg = aggregate_by_system(rows)
        self.assertEqual(list(agg.keys()), ['digestivo'])


# =====================================================================
# 3) score_system — fórmula, penalización y boost
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestScoreSystem(TransactionCase):

    def _stats(self, code='digestivo', sev=0, mod=0, lev=0):
        return {
            'sistema_code': code,
            'sistema_label': code,
            'severos': sev,
            'moderados': mod,
            'leves': lev,
            'sev_max_level': 'normal',
            'sev_max_score': 1,
            'hallazgos': [],
        }

    def test_formula_base(self):
        """2 severos + 3 moderados + 1 leve = 2*5 + 3*3 + 1*1 = 20."""
        info = score_system(self._stats(sev=2, mod=3, lev=1))
        self.assertEqual(info['score_base'], 20)
        self.assertEqual(info['score_final'], 20.0)
        self.assertFalse(info['penalty_applied'])
        self.assertFalse(info['boosted_por_antecedente'])

    def test_solo_severos(self):
        info = score_system(self._stats(sev=3))
        self.assertEqual(info['score_base'], 15)

    def test_solo_leves_dominantes_penaliza(self):
        """10 leves con 0 severos/moderados → leves dominan → penalty 0.5."""
        info = score_system(self._stats(lev=10))
        self.assertTrue(info['penalty_applied'])
        # base = 10, penalizado = 5.0
        self.assertEqual(info['score_final'], 5.0)

    def test_leves_no_dominantes_no_penaliza(self):
        """2 severos + 3 leves: leves no dominan (3 <= 4*2 = 8)."""
        info = score_system(self._stats(sev=2, mod=0, lev=3))
        self.assertFalse(info['penalty_applied'])

    def test_boost_multiplica_factor(self):
        """Sistema en boost_codes: score *= 1.6."""
        info = score_system(self._stats(sev=2), boost_codes=['digestivo'])
        # base = 10, boosted = 16.0
        self.assertTrue(info['boosted_por_antecedente'])
        self.assertEqual(info['boost_factor'], ANTECEDENTE_BOOST_FACTOR)
        self.assertEqual(info['score_final'], 16.0)

    def test_boost_no_aplica_a_otros_sistemas(self):
        info = score_system(
            self._stats(sev=2, code='digestivo'),
            boost_codes=['endocrino'],
        )
        self.assertFalse(info['boosted_por_antecedente'])
        self.assertEqual(info['score_final'], 10.0)

    def test_boost_y_penalty_se_combinan(self):
        """Leves dominantes + boost: base*0.5*1.6 = base*0.8."""
        info = score_system(
            self._stats(lev=10, code='digestivo'),
            boost_codes=['digestivo'],
        )
        # base 10, penalty → 5, boost → 8
        self.assertTrue(info['penalty_applied'])
        self.assertTrue(info['boosted_por_antecedente'])
        self.assertEqual(info['score_final'], 8.0)

    def test_auditoria_completa(self):
        """El breakdown debe ser auditable."""
        info = score_system(self._stats(sev=1, mod=1, lev=1))
        self.assertIn('weights', info)
        self.assertEqual(info['weights']['severos'], WEIGHTS['severo'])
        self.assertEqual(info['weights']['moderados'], WEIGHTS['moderado'])
        self.assertEqual(info['weights']['leves'], WEIGHTS['leve'])


# =====================================================================
# 4) rank() — API principal
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestRankAPI(TransactionCase):

    def test_rank_vacio(self):
        out = rank([])
        self.assertEqual(out['prioridades'], [])
        self.assertEqual(out['systems_audit'], {})
        self.assertEqual(out['boost_codes'], [])

    def test_rank_orden_por_score_desc(self):
        rows = [
            _mk('digestivo', 'leve'),
            _mk('endocrino', 'severo'),
            _mk('endocrino', 'severo'),
            _mk('nervioso', 'moderado'),
        ]
        out = rank(rows)
        codes = [p['sistema_code'] for p in out['prioridades']]
        # endocrino (10) > nervioso (3) > digestivo (1)
        self.assertEqual(codes[0], 'endocrino')
        self.assertEqual(codes[1], 'nervioso')
        self.assertEqual(codes[2], 'digestivo')

    def test_solo_sistemas_con_hallazgos_clinicos(self):
        """Sistemas con SOLO normales/desconocidos no aparecen en prioridades."""
        rows = [
            _mk('digestivo', 'severo'),
            _mk('endocrino', 'normal'),
            _mk('nervioso', 'desconocida'),
        ]
        out = rank(rows)
        codes = [p['sistema_code'] for p in out['prioridades']]
        self.assertIn('digestivo', codes)
        self.assertNotIn('endocrino', codes)
        self.assertNotIn('nervioso', codes)

    def test_max_priorities_tope(self):
        rows = []
        for i, code in enumerate(['digestivo', 'endocrino', 'nervioso',
                                  'cardiovascular', 'metabolico',
                                  'inmune', 'respiratorio', 'urinario']):
            rows.append(_mk(code, 'severo'))
        out = rank(rows, max_priorities=3)
        self.assertEqual(len(out['prioridades']), 3)
        # systems_audit conserva TODOS los sistemas con hallazgos
        self.assertEqual(len(out['systems_audit']), 8)

    def test_boost_reordena_ranking(self):
        """Sin antecedente: digestivo gana. Con antecedente endocrino:
        endocrino sube y puede empatar/superar."""
        rows = [
            _mk('digestivo', 'severo'),
            _mk('digestivo', 'severo'),     # base digestivo = 10
            _mk('endocrino', 'severo'),
            _mk('endocrino', 'moderado'),   # base endocrino = 8
        ]
        # Sin antecedente:
        out_sin = rank(rows, antecedente_text='')
        self.assertEqual(out_sin['prioridades'][0]['sistema_code'], 'digestivo')
        # Con antecedente 'hipotiroidismo' → endocrino *1.6 = 12.8
        out_con = rank(rows, antecedente_text='hipotiroidismo')
        self.assertEqual(out_con['prioridades'][0]['sistema_code'], 'endocrino')
        self.assertIn('endocrino', out_con['boost_codes'])

    def test_antecedente_no_reclasifica(self):
        """REGLA CRÍTICA: el antecedente JAMÁS cambia el sistema_code de
        un hallazgo. Solo reordena."""
        rows = [
            _mk('digestivo', 'severo', parametro='Helicobacter'),
            _mk('endocrino', 'leve', parametro='TSH'),
        ]
        out = rank(rows, antecedente_text='hipotiroidismo y diabetes')
        # Cada prioridad debe seguir teniendo su sistema_code original
        sistemas_en_prio = {p['sistema_code'] for p in out['prioridades']}
        self.assertIn('digestivo', sistemas_en_prio)
        self.assertIn('endocrino', sistemas_en_prio)
        # Y los parámetros NO cambian de sistema
        endo_prio = next(p for p in out['prioridades']
                         if p['sistema_code'] == 'endocrino')
        for h in endo_prio['hallazgos_clave']:
            self.assertEqual(h['sistema_code'], 'endocrino')

    def test_orden_determinístico_en_empate_score(self):
        """Empate en score → severidad_score desc → total hallazgos desc
        → sistema_label asc."""
        rows = [
            _mk('digestivo', 'moderado'),
            _mk('digestivo', 'leve'),     # base = 4
            _mk('endocrino', 'leve'),
            _mk('endocrino', 'leve'),
            _mk('endocrino', 'leve'),
            _mk('endocrino', 'leve'),     # base = 4
        ]
        out = rank(rows)
        # Mismo score 4, digestivo tiene severidad mayor (moderado vs leve)
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'digestivo')

    def test_prioridad_contiene_audit(self):
        rows = [_mk('digestivo', 'severo')]
        out = rank(rows, antecedente_text='detox')
        prio = out['prioridades'][0]
        self.assertIn('audit', prio)
        self.assertIn('weights', prio['audit'])
        self.assertIn('boost_factor', prio['audit'])
        self.assertIn('hallazgos_clave', prio)

    def test_hallazgos_clave_top_3(self):
        rows = [_mk('digestivo', 'severo', parametro='p%d' % i)
                for i in range(7)]
        out = rank(rows)
        prio = out['prioridades'][0]
        self.assertEqual(len(prio['hallazgos_clave']), 3)


# =====================================================================
# 5) End-to-end multicaso (classifier + severity + ranker)
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestMulticasoConRanker(TransactionCase):
    """Cada caso A-F debe producir un ranking coherente con su perfil."""

    def test_caso_a_digestivo_y_toxicidad_top(self):
        """Caso A: digestivo + metales. Con antecedente 'detox' ambos
        suben."""
        with_sev = _pipeline('A')
        out = rank(with_sev, antecedente_text='detox metales pesados')
        codes = [p['sistema_code'] for p in out['prioridades'][:2]]
        self.assertIn('toxicidad', codes)
        self.assertIn('digestivo', codes)
        self.assertIn('toxicidad', out['boost_codes'])

    def test_caso_b_metabolico_top(self):
        with_sev = _pipeline('B')
        out = rank(with_sev)
        self.assertGreaterEqual(len(out['prioridades']), 1)
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'metabolico')

    def test_caso_b_diabetes_refuerza_metabolico(self):
        """Con antecedente 'diabetes', metabolico se refuerza aún más."""
        with_sev = _pipeline('B')
        out_sin = rank(with_sev)
        out_con = rank(with_sev, antecedente_text='Diabetes tipo 2')
        # metabolico debe seguir primero (ya era top) pero con score boosted
        prio_sin = next(p for p in out_sin['prioridades']
                        if p['sistema_code'] == 'metabolico')
        prio_con = next(p for p in out_con['prioridades']
                        if p['sistema_code'] == 'metabolico')
        self.assertGreater(prio_con['score'], prio_sin['score'])
        self.assertTrue(prio_con['boosted_por_antecedente'])

    def test_caso_c_endocrino_top(self):
        with_sev = _pipeline('C')
        out = rank(with_sev, antecedente_text='hipotiroidismo')
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'endocrino')
        self.assertIn('endocrino', out['boost_codes'])

    def test_caso_d_musculo_o_inmune_top(self):
        """Caso D: artritis → musculo + inmune."""
        with_sev = _pipeline('D')
        out = rank(with_sev, antecedente_text='artritis crónica')
        top_codes = [p['sistema_code'] for p in out['prioridades'][:2]]
        self.assertTrue(
            'musculoesqueletico' in top_codes or 'inmune' in top_codes,
            "Caso D: esperaba musculo o inmune en top 2, got %r" % top_codes,
        )

    def test_caso_e_nervioso_top(self):
        with_sev = _pipeline('E')
        out = rank(with_sev, antecedente_text='ansiedad e insomnio')
        self.assertEqual(out['prioridades'][0]['sistema_code'], 'nervioso')

    def test_caso_f_sin_antecedente_no_boost(self):
        """Caso F sin antecedente: boost_codes vacío."""
        with_sev = _pipeline('F')
        out = rank(with_sev, antecedente_text='')
        self.assertEqual(out['boost_codes'], [])
        # Aun así debe haber prioridades (hay anormales)
        self.assertGreater(len(out['prioridades']), 0)

    def test_casos_producen_firmas_distintas(self):
        """Validación anti-contaminación cross-case: cada perfil produce
        un sistema TOP distinto (excepto F que es mezcla)."""
        top = {}
        antecedentes = {
            'A': 'detox',
            'B': 'diabetes',
            'C': 'hipotiroidismo',
            'D': 'artritis',
            'E': 'ansiedad',
        }
        for k, ant in antecedentes.items():
            with_sev = _pipeline(k)
            out = rank(with_sev, antecedente_text=ant)
            self.assertGreater(len(out['prioridades']), 0,
                               "Caso %s sin prioridades" % k)
            top[k] = out['prioridades'][0]['sistema_code']
        # Esperamos que A,B,C,D,E tengan tops distintos (motor generalista)
        unique_tops = set(top.values())
        self.assertGreaterEqual(
            len(unique_tops), 4,
            "Esperaba >=4 tops distintos entre A-E, got: %r" % top,
        )

    def test_systems_audit_contiene_todos_los_sistemas(self):
        """systems_audit conserva TODOS los sistemas con hallazgos clínicos,
        no solo top-N. Esto permite auditoría completa."""
        with_sev = _pipeline('D')
        out = rank(with_sev, max_priorities=1)
        self.assertEqual(len(out['prioridades']), 1)
        # Debe haber más sistemas en systems_audit
        self.assertGreaterEqual(len(out['systems_audit']), 2)


# =====================================================================
# 6) Aislamiento del flujo productivo
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestRankerAislamiento(TransactionCase):

    def test_ranker_no_importa_odoo(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/ranker.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        for forbidden in ('from odoo', 'import odoo', 'self.env',
                          'sudo()', 'ir.model'):
            self.assertNotIn(forbidden, content,
                             "ranker.py acopla con Odoo: %r" % forbidden)

    def test_ranker_no_es_usado_por_parser_productivo(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/parser.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .ranker', content)
        self.assertNotIn('import ranker', content)

    def test_ranker_no_es_usado_por_master_summary(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/master_summary.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .ranker', content)

    def test_ranker_no_es_usado_por_archivo_cliente(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/valoracion_archivo_cliente.py',
        )
        with open(path, 'r') as f:
            content = f.read()
        self.assertNotIn('from .biocuantico.ranker', content)
        self.assertNotIn('from .biocuantico import ranker', content)

    def test_ranker_no_referencia_clientes_ni_productos(self):
        """Ranker es puro: no debe nombrar clientes, IA, productos ni
        cotizaciones."""
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../models/biocuantico/ranker.py',
        )
        with open(path, 'r') as f:
            content = f.read().lower()
        for forbidden in ('gema', 'openai', 'anthropic', 'claude api',
                          'sale_order', 'cotizacion', 'product.template',
                          'vital pro', 'paciente'):
            self.assertNotIn(forbidden, content,
                             "ranker.py contiene %r" % forbidden)

    def test_ranker_no_muta_sistema_code_de_hallazgos(self):
        """REGLA CRÍTICA: el ranker no debe alterar el sistema_code de
        ningún hallazgo. Verificamos con un caso con antecedente fuerte."""
        rows = [
            _mk('digestivo', 'severo', parametro='Helicobacter'),
            _mk('endocrino', 'severo', parametro='TSH'),
        ]
        out = rank(rows, antecedente_text='hipotiroidismo')
        # Los hallazgos_clave conservan sus sistema_code originales
        for prio in out['prioridades']:
            for h in prio['hallazgos_clave']:
                self.assertEqual(h['sistema_code'], prio['sistema_code'],
                                 "Hallazgo reclasificado: %r" % h)


# =====================================================================
# 7) Versión del manifest
# =====================================================================
@tagged('post_install', '-at_install', 'valoracion', 'biocuantico', 'motor_v3')
class TestManifest314(TransactionCase):

    def test_version_2_1_3_o_superior(self):
        modulo = self.env['ir.module.module'].search(
            [('name', '=', 'valoracion')], limit=1,
        )
        self.assertTrue(modulo)
        v = modulo.latest_version or ''
        parts = v.split('.')
        if len(parts) >= 5:
            x, y, z = int(parts[2]), int(parts[3]), int(parts[4])
            self.assertGreaterEqual(
                (x, y, z), (2, 1, 3),
                "Manifest %s < 18.0.2.1.3 (Fase 3.4)" % v,
            )

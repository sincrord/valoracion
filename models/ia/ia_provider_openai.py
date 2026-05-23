# -*- coding: utf-8 -*-
"""Proveedor IA usando la Responses API de OpenAI.

Endpoint: https://api.openai.com/v1/responses

Payload (formato simple):
    {
        "model":              <str>,
        "input":              <str con system+user combinados>,
        "temperature":        <float>,
        "max_output_tokens":  <int>
    }

Headers:
    Authorization: Bearer <api_key.strip()>
    Content-Type:  application/json

NOTA: la API Key SIEMPRE se hace .strip() antes de usarse para evitar
falsos 401 por whitespace residual al pegar la key en la UI.

NOTA: en caso de HTTP 401, el provider devuelve un error_message que
incluye la api_key ENMASCARADA (formato sk-...abc1) — los primeros 8 y
últimos 4 caracteres únicamente. NUNCA se loggea la key completa.

Como la Responses API simple NO usa tool_use ni structured outputs en
este payload, instruimos al modelo en el propio input para que devuelva
JSON puro. Luego parseamos la respuesta y validamos campos requeridos
manualmente (igual que en Claude).

Las imágenes (input multimodal) NO se soportan con este formato simple.
Si se pasan, se ignoran y se registra warning en el log.

Manejo de errores idéntico a ClaudeProvider:
  * Timeout                  -> estado='timeout'
  * 401                      -> estado='error_api' (key enmascarada en mensaje)
  * 429                      -> estado='error_api' (rate limit)
  * 400                      -> estado='error_api' (request inválido)
  * Otros HTTP !=2xx         -> estado='error_api'
  * JSON no parseable        -> estado='error_parsing'
  * Sin output_text          -> estado='error_parsing'
  * Campos requeridos faltan -> estado='error_validacion'

NUNCA propaga excepciones. Siempre devuelve dict normalizado vía make_result().
"""
import json
import logging
import time

try:
    import requests  # type: ignore
except ImportError:
    requests = None

from .ia_provider_base import (
    IAProviderBase,
    make_result,
    ESTADO_EXITO,
    ESTADO_ERROR_API,
    ESTADO_TIMEOUT,
    ESTADO_ERROR_PARSING,
    ESTADO_ERROR_VALIDACION,
)

_logger = logging.getLogger(__name__)


# Cap del raw_response para no inflar la BD si la respuesta es enorme
RAW_RESPONSE_CAP = 50_000


class OpenAIProvider(IAProviderBase):
    """Implementación del provider para OpenAI (Responses API)."""

    name = 'openai'
    DEFAULT_ENDPOINT = 'https://api.openai.com/v1/responses'

    # ====================================================================
    # Helpers de seguridad
    # ====================================================================
    def _stripped_api_key(self):
        """Devuelve la api_key sin whitespace al inicio/final.

        Centralizar el strip evita falsos 401 cuando el usuario pega la
        key con un espacio o salto de línea accidental.
        """
        return (self.api_key or '').strip()

    def _masked_api_key(self):
        """Devuelve la api_key enmascarada para usar en logs.

        Formato: <primeros 8 chars>...<últimos 4 chars>
        Ejemplo: sk-proj-...abc1

        Si la key es muy corta o vacía, devuelve '***' sin exponer nada.
        """
        key = self._stripped_api_key()
        if not key:
            return '(vacía)'
        if len(key) <= 12:
            return '***'
        return '%s...%s' % (key[:8], key[-4:])

    # ====================================================================
    # Construcción del input combinado
    # ====================================================================
    def _build_combined_input(self, system_prompt, user_prompt, output_schema):
        """Combina system+user+esquema JSON en un solo string para 'input'.

        La Responses API en su forma simple acepta 'input' como string.
        El system_prompt del template original instruye al modelo a usar
        una herramienta (tool_use, formato Claude). Aquí lo sobrescribimos
        instruyendo a OpenAI a devolver JSON puro conforme al schema.
        """
        try:
            schema_json = json.dumps(
                output_schema or {}, ensure_ascii=False, indent=2,
            )
        except (TypeError, ValueError):
            schema_json = '{}'

        return (
            "%s\n\n"
            "%s\n\n"
            "=== INSTRUCCIÓN DE FORMATO PARA OPENAI ===\n"
            "Ignora cualquier instrucción anterior sobre 'invocar herramienta' "
            "o 'tool_use'. Responde EXCLUSIVAMENTE con un objeto JSON válido "
            "(sin markdown, sin bloques ```json, sin texto antes ni después) "
            "que cumpla EXACTAMENTE este JSON Schema:\n\n"
            "%s\n\n"
            "Tu respuesta debe empezar con '{' y terminar con '}'."
        ) % (system_prompt or '', user_prompt or '', schema_json)

    # ====================================================================
    # Limpieza de la respuesta (quitar fences markdown si aparecen)
    # ====================================================================
    @staticmethod
    def _strip_markdown_fences(text):
        """Si el modelo devolvió ```json ... ``` por error, lo limpia."""
        if not text:
            return text
        cleaned = text.strip()
        if cleaned.startswith('```'):
            # quitar fence de apertura
            after_open = cleaned[3:]
            # opcional: lenguaje (json, etc.)
            nl = after_open.find('\n')
            if nl != -1:
                first_line = after_open[:nl].strip().lower()
                if first_line in ('json', 'javascript', ''):
                    after_open = after_open[nl + 1:]
            # quitar fence de cierre
            close_idx = after_open.rfind('```')
            if close_idx != -1:
                after_open = after_open[:close_idx]
            cleaned = after_open.strip()
        return cleaned

    # ====================================================================
    # API pública
    # ====================================================================
    def generate(self, system_prompt, user_prompt, output_schema, images=None):
        """Ejecuta la llamada a OpenAI Responses API.

        Ver IAProviderBase.generate para contrato completo.
        """
        if requests is None:
            return make_result(
                False,
                error="Falta librería 'requests'. Instala: pip3 install requests",
                estado=ESTADO_ERROR_API,
            )

        # Aviso si vienen imágenes (no soportadas en este formato simple)
        if images:
            _logger.warning(
                "OpenAIProvider: %d imagen(es) ignoradas. La Responses API "
                "en formato 'input': str no soporta multimodal.",
                len(images),
            )

        endpoint = self.endpoint_url or self.DEFAULT_ENDPOINT
        api_key = self._stripped_api_key()
        masked = self._masked_api_key()

        # Construir input combinado (system + user + instrucción de JSON)
        combined_input = self._build_combined_input(
            system_prompt, user_prompt, output_schema,
        )

        payload = {
            'model': self.model,
            'input': combined_input,
            'temperature': self.temperature,
            'max_output_tokens': self.max_tokens,
        }

        headers = {
            'Authorization': 'Bearer %s' % api_key,
            'Content-Type': 'application/json',
        }

        # ============================================================
        # RATE LIMIT PRE-EMPTIVO
        # ============================================================
        # Espera fija de 2 segundos ANTES de cada llamada para evitar
        # bursts cuando el usuario genera varias valoraciones seguidas.
        # Mitigación proactiva del 429.
        time.sleep(2)

        # ============================================================
        # RETRY LOOP — solo en HTTP 429
        # ============================================================
        # Schedule de espera ADICIONAL antes de cada intento (después del
        # sleep(2) base). Backoff:
        #   intento 1: 0s adicionales (sleep base 2s ya ocurrió)
        #   intento 2: 2s adicionales antes de reintentar
        #   intento 3: 5s adicionales antes de reintentar
        # Solo se reintenta en HTTP 429.
        # 401 y 403 se devuelven INMEDIATAMENTE sin reintento.
        # ============================================================
        RETRY_SCHEDULE = [0, 2, 5]
        MAX_ATTEMPTS = len(RETRY_SCHEDULE)

        resp = None
        duration_ms = 0
        raw_text = ''

        for attempt, extra_sleep in enumerate(RETRY_SCHEDULE, start=1):
            if extra_sleep > 0:
                _logger.warning(
                    "OpenAIProvider: reintento %d/%d tras HTTP 429 | "
                    "esperando %ds | endpoint=%s | modelo=%s | key=%s",
                    attempt, MAX_ATTEMPTS, extra_sleep,
                    endpoint, self.model, masked,
                )
                time.sleep(extra_sleep)

            start = time.time()
            try:
                resp = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout:
                duration_ms = int((time.time() - start) * 1000)
                _logger.warning(
                    "OpenAIProvider: timeout intento=%d/%d tras %ss en %s",
                    attempt, MAX_ATTEMPTS, self.timeout, endpoint,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error=(
                        "Timeout tras %s segundos (intento %d/%d). "
                        "Aumenta el timeout en configuración o reintenta "
                        "más tarde."
                    ) % (self.timeout, attempt, MAX_ATTEMPTS),
                    estado=ESTADO_TIMEOUT,
                )
            except requests.exceptions.ConnectionError as e:
                duration_ms = int((time.time() - start) * 1000)
                _logger.warning(
                    "OpenAIProvider: conexión fallida intento=%d/%d: %s",
                    attempt, MAX_ATTEMPTS, e,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error="Error de conexión con OpenAI (intento %d/%d): %s"
                          % (attempt, MAX_ATTEMPTS, e),
                    estado=ESTADO_ERROR_API,
                )
            except requests.exceptions.RequestException as e:
                duration_ms = int((time.time() - start) * 1000)
                _logger.exception(
                    "OpenAIProvider: excepción HTTP intento=%d", attempt,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error="Error de petición (intento %d/%d): %s"
                          % (attempt, MAX_ATTEMPTS, e),
                    estado=ESTADO_ERROR_API,
                )

            duration_ms = int((time.time() - start) * 1000)
            raw_text = resp.text or ''
            last_status = resp.status_code

            # Log estructurado por intento (NUNCA incluye key completa)
            _logger.info(
                "OpenAIProvider HTTP %d | intento=%d/%d | endpoint=%s | "
                "modelo=%s | key=%s | duration_ms=%d",
                last_status, attempt, MAX_ATTEMPTS,
                endpoint, self.model, masked, duration_ms,
            )

            # ============================================================
            # 401 — NUNCA reintentar. Fail inmediato.
            # ============================================================
            if last_status == 401:
                _logger.warning(
                    "OpenAIProvider 401 Unauthorized | intento=%d | "
                    "proveedor=openai | key=%s | modelo=%s | endpoint=%s",
                    attempt, masked, self.model, endpoint,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error=(
                        "API Key de OpenAI rechazada (HTTP 401). Diagnóstico:\n"
                        "  • status_code : 401\n"
                        "  • intento     : %d/%d (sin reintento, fallo inmediato)\n"
                        "  • proveedor   : openai\n"
                        "  • modelo      : %s\n"
                        "  • endpoint    : %s\n"
                        "  • api_key     : %s\n"
                        "Si el curl funciona pero Odoo no, comprueba que la "
                        "key en Settings → Valoración → OpenAI no tenga "
                        "espacios pegados. La key se hace .strip() antes de enviar."
                    ) % (attempt, MAX_ATTEMPTS, self.model, endpoint, masked),
                    estado=ESTADO_ERROR_API,
                    raw_response=raw_text[:RAW_RESPONSE_CAP],
                )

            # ============================================================
            # 403 — NUNCA reintentar. Fail inmediato.
            # ============================================================
            if last_status == 403:
                _logger.warning(
                    "OpenAIProvider 403 Forbidden | intento=%d | "
                    "proveedor=openai | key=%s | modelo=%s | endpoint=%s",
                    attempt, masked, self.model, endpoint,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error=(
                        "Acceso denegado por OpenAI (HTTP 403). Diagnóstico:\n"
                        "  • status_code : 403\n"
                        "  • intento     : %d/%d (sin reintento, fallo inmediato)\n"
                        "  • proveedor   : openai\n"
                        "  • modelo      : %s\n"
                        "  • endpoint    : %s\n"
                        "  • api_key     : %s\n"
                        "Tu API Key es válida pero no tiene permiso para usar "
                        "este modelo o endpoint. Verifica plan/permisos en "
                        "tu cuenta OpenAI."
                    ) % (attempt, MAX_ATTEMPTS, self.model, endpoint, masked),
                    estado=ESTADO_ERROR_API,
                    raw_response=raw_text[:RAW_RESPONSE_CAP],
                )

            # ============================================================
            # 429 — reintentar si quedan intentos; si no, fail.
            # ============================================================
            if last_status == 429:
                if attempt < MAX_ATTEMPTS:
                    continue  # próxima iteración esperará el extra_sleep
                # Reintentos agotados
                _logger.warning(
                    "OpenAIProvider 429 agotado tras %d intentos | "
                    "endpoint=%s | modelo=%s | key=%s",
                    MAX_ATTEMPTS, endpoint, self.model, masked,
                )
                return make_result(
                    False,
                    duration_ms=duration_ms,
                    error=(
                        "Límite de tasa de OpenAI excedido (HTTP 429). "
                        "Se reintentó automáticamente con backoff de 2s y 5s. "
                        "Diagnóstico:\n"
                        "  • status_code : 429\n"
                        "  • intentos    : %d/%d (agotados)\n"
                        "  • proveedor   : openai\n"
                        "  • modelo      : %s\n"
                        "  • endpoint    : %s\n"
                        "  • api_key     : %s\n"
                        "Espera unos minutos y reintenta manualmente."
                    ) % (MAX_ATTEMPTS, MAX_ATTEMPTS,
                         self.model, endpoint, masked),
                    estado=ESTADO_ERROR_API,
                    raw_response=raw_text[:RAW_RESPONSE_CAP],
                )

            # Éxito o error no-retryable distinto a 401/403/429:
            # salimos del loop y procesamos abajo
            break

        # ============================================================
        # Manejo de códigos HTTP de error restantes (no retryables)
        # ============================================================
        if resp.status_code == 400:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Petición inválida (HTTP 400). Posibles causas: "
                      "modelo inexistente, formato de input incorrecto, "
                      "contexto excedido. Detalle: %s" % raw_text[:500],
                estado=ESTADO_ERROR_API,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )
        if resp.status_code == 404:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Endpoint o modelo no encontrado (HTTP 404). "
                      "Verifica que el endpoint sea correcto "
                      "(esperado: %s) y que el modelo '%s' exista en tu "
                      "cuenta OpenAI." % (self.DEFAULT_ENDPOINT, self.model),
                estado=ESTADO_ERROR_API,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )
        if not resp.ok:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Error HTTP %s: %s" % (resp.status_code, raw_text[:500]),
                estado=ESTADO_ERROR_API,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        # ============================================================
        # Parsear JSON de la respuesta de OpenAI
        # ============================================================
        try:
            body = resp.json()
        except ValueError as e:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Respuesta de OpenAI no es JSON: %s" % e,
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        # Métricas de tokens (Responses API usa input_tokens / output_tokens)
        usage = body.get('usage') or {}
        tokens_input = int(usage.get('input_tokens') or 0)
        tokens_output = int(usage.get('output_tokens') or 0)

        # ============================================================
        # Extraer el texto generado
        # ============================================================
        # Responses API expone 'output_text' como conveniencia que junta
        # todos los bloques output_text. Si no existe, navegamos la
        # estructura completa.
        output_text = body.get('output_text') or ''
        if not output_text:
            for out_item in body.get('output') or []:
                if not isinstance(out_item, dict):
                    continue
                if out_item.get('type') == 'message':
                    for c in out_item.get('content') or []:
                        if isinstance(c, dict) and c.get('type') == 'output_text':
                            output_text = (output_text or '') + (c.get('text') or '')

        if not output_text:
            return make_result(
                False,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                error="OpenAI no devolvió texto en la respuesta. "
                      "Estado: %s" % (body.get('status') or '(desconocido)'),
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        # Limpiar fences markdown si el modelo los añadió por error
        cleaned = self._strip_markdown_fences(output_text)

        # ============================================================
        # Parsear el JSON
        # ============================================================
        try:
            data = json.loads(cleaned)
        except ValueError as e:
            return make_result(
                False,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                error="OpenAI no devolvió JSON válido: %s. "
                      "Primeros 300 chars: %s" % (e, cleaned[:300]),
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        if not isinstance(data, dict):
            return make_result(
                False,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                error="OpenAI devolvió JSON pero no es un objeto.",
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        # Validar campos requeridos del schema
        if isinstance(output_schema, dict):
            required = output_schema.get('required') or []
            missing = [k for k in required if k not in data]
            if missing:
                return make_result(
                    False,
                    data=data,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    duration_ms=duration_ms,
                    error="Campos requeridos faltantes en respuesta IA: %s" % missing,
                    estado=ESTADO_ERROR_VALIDACION,
                    raw_response=raw_text[:RAW_RESPONSE_CAP],
                )

        # Éxito
        return make_result(
            True,
            data=data,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            duration_ms=duration_ms,
            estado=ESTADO_EXITO,
            raw_response=raw_text[:RAW_RESPONSE_CAP],
        )

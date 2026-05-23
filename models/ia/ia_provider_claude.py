# -*- coding: utf-8 -*-
"""Proveedor IA usando la Messages API de Anthropic Claude.

Estrategia clave: se usa el mecanismo "tool_use" con tool_choice forzada.
Esto obliga al modelo a devolver la salida estructurada conforme al JSON
Schema (output_schema), eliminando el riesgo de JSON inválido o texto
narrativo extra (riesgo #2 de la propuesta técnica).

Manejo de errores:
  * Timeout         -> estado='timeout'
  * 401             -> estado='error_api', mensaje sobre API key inválida
  * 429             -> estado='error_api', mensaje sobre rate limit
  * Otros HTTP !=2xx-> estado='error_api'
  * JSON no parseable-> estado='error_parsing'
  * Sin tool_use   -> estado='error_parsing'
  * Campos faltantes-> estado='error_validacion'

NUNCA propaga excepciones. Siempre devuelve dict normalizado.
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


class ClaudeProvider(IAProviderBase):
    """Implementación del provider para Claude (Anthropic Messages API)."""

    name = 'claude'
    DEFAULT_ENDPOINT = 'https://api.anthropic.com/v1/messages'
    ANTHROPIC_VERSION = '2023-06-01'
    TOOL_NAME = 'generar_valoracion'
    TOOL_DESCRIPTION = (
        'Devuelve la valoración funcional VitalHealth como JSON estructurado. '
        'Usa esta herramienta SIEMPRE para responder; no escribas texto fuera '
        'de la herramienta.'
    )

    # ====================================================================
    # API pública
    # ====================================================================
    def generate(self, system_prompt, user_prompt, output_schema, images=None):
        """Ejecuta la llamada a Claude.

        Ver IAProviderBase.generate para contrato completo.
        """
        # Verificar dependencia
        if requests is None:
            return make_result(
                False,
                error="Falta librería 'requests'. Instala: pip3 install requests",
                estado=ESTADO_ERROR_API,
            )

        endpoint = self.endpoint_url or self.DEFAULT_ENDPOINT

        # Construir contenido del mensaje (texto + imágenes opcionales)
        content = [{'type': 'text', 'text': user_prompt or ''}]
        for img in (images or []):
            b64 = img.get('image_b64')
            media_type = img.get('image_media_type')
            if not b64 or not media_type:
                continue
            content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': media_type,
                    'data': b64,
                },
            })

        # Tool definition para forzar JSON estructurado
        tool = {
            'name': self.TOOL_NAME,
            'description': self.TOOL_DESCRIPTION,
            'input_schema': output_schema or {'type': 'object', 'properties': {}},
        }

        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
            'system': system_prompt or '',
            'messages': [{'role': 'user', 'content': content}],
            'tools': [tool],
            'tool_choice': {'type': 'tool', 'name': self.TOOL_NAME},
        }

        headers = {
            'x-api-key': self.api_key,
            'anthropic-version': self.ANTHROPIC_VERSION,
            'content-type': 'application/json',
        }

        # Llamada HTTP con manejo robusto
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
                "ClaudeProvider: timeout tras %ss en %s", self.timeout, endpoint,
            )
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Timeout tras %s segundos. Aumenta el timeout en "
                      "configuración o reintenta más tarde." % self.timeout,
                estado=ESTADO_TIMEOUT,
            )
        except requests.exceptions.ConnectionError as e:
            duration_ms = int((time.time() - start) * 1000)
            _logger.warning("ClaudeProvider: error de conexión: %s", e)
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Error de conexión con el proveedor IA: %s" % e,
                estado=ESTADO_ERROR_API,
            )
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.time() - start) * 1000)
            _logger.exception("ClaudeProvider: excepción HTTP")
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Error de petición: %s" % e,
                estado=ESTADO_ERROR_API,
            )

        duration_ms = int((time.time() - start) * 1000)
        raw_text = resp.text or ''

        # Manejo de códigos HTTP de error específicos
        if resp.status_code == 401:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="API Key inválida o no autorizada (HTTP 401). "
                      "Revisa la configuración del módulo.",
                estado=ESTADO_ERROR_API,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )
        if resp.status_code == 429:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Límite de tasa excedido (HTTP 429). "
                      "Espera unos minutos y reintenta.",
                estado=ESTADO_ERROR_API,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )
        if resp.status_code == 400:
            # Bad request: probablemente prompt mal formado o demasiado grande
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Petición inválida (HTTP 400). Posibles causas: "
                      "prompt demasiado grande, schema mal formado o "
                      "modelo inexistente. Detalle: %s" % raw_text[:500],
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

        # Parsear JSON de la respuesta
        try:
            body = resp.json()
        except ValueError as e:
            return make_result(
                False,
                duration_ms=duration_ms,
                error="Respuesta del proveedor no es JSON: %s" % e,
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        # Métricas de tokens (Anthropic devuelve usage)
        usage = body.get('usage') or {}
        tokens_input = int(usage.get('input_tokens') or 0)
        tokens_output = int(usage.get('output_tokens') or 0)

        # Buscar el bloque tool_use con la herramienta esperada
        contents = body.get('content') or []
        tool_block = next(
            (c for c in contents
             if isinstance(c, dict)
             and c.get('type') == 'tool_use'
             and c.get('name') == self.TOOL_NAME),
            None,
        )
        if not tool_block:
            # Recoger texto plano si lo hubiera (a veces el modelo responde
            # con texto en vez de invocar la herramienta)
            text_blocks = [
                c.get('text', '') for c in contents
                if isinstance(c, dict) and c.get('type') == 'text'
            ]
            text_dump = ' '.join(text_blocks)[:500]
            return make_result(
                False,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                error="La IA no invocó la herramienta esperada '%s'. "
                      "Texto devuelto: %s" % (self.TOOL_NAME, text_dump or '(vacío)'),
                estado=ESTADO_ERROR_PARSING,
                raw_response=raw_text[:RAW_RESPONSE_CAP],
            )

        data = tool_block.get('input') or {}
        if not isinstance(data, dict):
            return make_result(
                False,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                error="La invocación de herramienta no contiene un objeto "
                      "JSON válido en 'input'.",
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

# -*- coding: utf-8 -*-
"""Subpaquete de integración con IA.

Aísla la lógica de extracción de archivos y la comunicación con proveedores
externos. No define modelos Odoo; expone clases utilitarias y la factory
build_provider() que el orquestador (valoracion.valoracion._ejecutar_generacion_ia)
usa para instanciar el proveedor configurado.

Para añadir un nuevo proveedor en el futuro:
  1. Crear models/ia/ia_provider_<nombre>.py heredando de IAProviderBase.
  2. Añadir el import abajo: `from . import ia_provider_<nombre>`.
  3. Añadir la rama elif correspondiente en build_provider().
  4. Añadir los campos en res_config_settings.py + entrada en la
     selection valoracion_ia_proveedor.
  5. Añadir el bloque en res_config_settings_views.xml.
"""
from . import file_extractor
from . import context_builder
from . import catalog_enricher
from . import ia_provider_base
from . import ia_provider_claude
from . import ia_provider_openai


# ============================================================
# Factory: build_provider(env)
# ============================================================
# El orquestador en valoracion_valoracion._ejecutar_generacion_ia llama a
# esta factory para obtener una instancia del proveedor configurado en
# res.config.settings (campo valoracion_ia_proveedor).
#
# Cada provider construido lleva atributos extra (costo_per_1m_input/output)
# que el orquestador usa para calcular el costo estimado por llamada.
# ============================================================

def build_provider(env):
    """Devuelve una instancia del proveedor IA configurado.

    Lee de ir.config_parameter:
      * valoracion.ia_proveedor: 'claude' | 'openai'
      * Para Claude:
          - valoracion.ia_api_key
          - valoracion.ia_endpoint_url
          - valoracion.ia_modelo
          - valoracion.ia_costo_per_1m_input
          - valoracion.ia_costo_per_1m_output
      * Para OpenAI:
          - valoracion.ia_openai_api_key
          - valoracion.ia_openai_endpoint_url
          - valoracion.ia_openai_modelo
          - valoracion.ia_openai_costo_per_1m_input
          - valoracion.ia_openai_costo_per_1m_output
      * Compartidos (todos los proveedores):
          - valoracion.ia_temperatura
          - valoracion.ia_max_tokens
          - valoracion.ia_timeout_seconds

    Lanza:
        IAProviderConfigError si el proveedor está mal configurado
        (api_key vacía o modelo vacío).
        ValueError si el nombre de proveedor no es soportado.
    """
    Param = env['ir.config_parameter'].sudo()
    proveedor = (Param.get_param('valoracion.ia_proveedor', 'claude') or 'claude').lower()

    # Parámetros comunes a todos los proveedores
    temperatura = float(Param.get_param('valoracion.ia_temperatura', '0.3') or 0.3)
    max_tokens = int(Param.get_param('valoracion.ia_max_tokens', '4096') or 4096)
    timeout = int(Param.get_param('valoracion.ia_timeout_seconds', '120') or 120)

    if proveedor == 'claude':
        from .ia_provider_claude import ClaudeProvider
        provider = ClaudeProvider(
            api_key=Param.get_param('valoracion.ia_api_key'),
            endpoint_url=(
                Param.get_param('valoracion.ia_endpoint_url')
                or 'https://api.anthropic.com/v1/messages'
            ),
            model=Param.get_param('valoracion.ia_modelo') or 'claude-sonnet-4-5',
            temperature=temperatura,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        provider.costo_per_1m_input = float(
            Param.get_param('valoracion.ia_costo_per_1m_input', '3.0') or 3.0
        )
        provider.costo_per_1m_output = float(
            Param.get_param('valoracion.ia_costo_per_1m_output', '15.0') or 15.0
        )
        return provider

    if proveedor == 'openai':
        from .ia_provider_openai import OpenAIProvider
        provider = OpenAIProvider(
            api_key=Param.get_param('valoracion.ia_openai_api_key'),
            endpoint_url=(
                Param.get_param('valoracion.ia_openai_endpoint_url')
                or 'https://api.openai.com/v1/chat/completions'
            ),
            model=Param.get_param('valoracion.ia_openai_modelo') or 'gpt-4o',
            temperature=temperatura,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        provider.costo_per_1m_input = float(
            Param.get_param('valoracion.ia_openai_costo_per_1m_input', '2.5') or 2.5
        )
        provider.costo_per_1m_output = float(
            Param.get_param('valoracion.ia_openai_costo_per_1m_output', '10.0') or 10.0
        )
        return provider

    raise ValueError(
        "Proveedor IA no soportado: '%s'. Valores válidos: 'claude', 'openai'."
        % proveedor
    )

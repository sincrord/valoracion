/** @odoo-module **/

/**
 * Overlay de carga durante la generación de valoraciones con IA.
 *
 * BUG previo (corregido): si el botón tenía confirm="...", el overlay aparecía
 * ANTES de la confirmación y bloqueaba el diálogo. Esto pasaba porque el JS
 * triggereaba el overlay con el clic en el botón.
 *
 * Solución actual: el overlay se dispara por la llamada RPC real al método
 * Python, no por el clic del botón. Así:
 *   - Botón sin confirm  → clic → RPC inmediata → overlay aparece.
 *   - Botón con confirm  → clic → diálogo de confirmación → si el usuario
 *                          confirma → RPC → overlay aparece.
 *                        → si el usuario cancela → no hay RPC → no hay overlay.
 *
 * Detección de la RPC:
 *   - URL: /web/dataset/call_kw/valoracion.valoracion/<METHOD>
 *   - URL: /web/dataset/call_button (con method en el body)
 *
 * Auto-cierre: cuando todas las peticiones activas terminan (éxito o error),
 * el overlay se oculta. Si fue error, el modal de error de Odoo aparece
 * sin overlay encima.
 *
 * Failsafe: timeout duro de 4 minutos para nunca dejar overlay zombie.
 */

import { registry } from "@web/core/registry";

const VALORACION_MODEL = "valoracion.valoracion";
const VALORACION_METHODS = [
    "action_generar_valoracion",
    "action_regenerar",
];

const MESSAGES = [
    "Analizando información del cliente…",
    "Procesando archivos…",
    "Generando valoración con IA…",
    "Esto puede tardar unos segundos…",
];

const LONG_RUNNING_MSG = "La IA sigue procesando información compleja…";
const ROTATE_MS = 3500;
const LONG_THRESHOLD_MS = 15000;
const HARD_TIMEOUT_MS = 240000;  // 4 min failsafe

let overlayEl = null;
let active = false;
let pendingRequests = 0;
let rotateTimer = null;
let longTimer = null;
let hardTimer = null;
let messageIndex = 0;

// =================================================================
// Detección: ¿esta llamada RPC es a uno de nuestros métodos target?
// =================================================================
function isTargetRpc(url, body) {
    if (!url) {
        return false;
    }

    // Patrón 1: URL directa
    //   /web/dataset/call_kw/valoracion.valoracion/action_generar_valoracion
    //   /web/dataset/call_kw/valoracion.valoracion/action_regenerar
    if (url.indexOf("/web/dataset/call_kw/" + VALORACION_MODEL + "/") !== -1) {
        for (let i = 0; i < VALORACION_METHODS.length; i++) {
            const m = VALORACION_METHODS[i];
            // El método aparece como segmento final del path o seguido por '?'
            if (url.endsWith("/" + m) ||
                url.indexOf("/" + m + "?") !== -1 ||
                url.indexOf("/" + m + "/") !== -1) {
                return true;
            }
        }
    }

    // Patrón 2: /web/dataset/call_button con method en el body JSON-RPC
    if (url.indexOf("/web/dataset/call_button") !== -1) {
        try {
            const data = typeof body === "string" ? JSON.parse(body) : null;
            if (data && data.params &&
                data.params.model === VALORACION_MODEL &&
                VALORACION_METHODS.indexOf(data.params.method) !== -1) {
                return true;
            }
        } catch (e) {
            // Body no parseable: ignorar
        }
    }

    return false;
}

// =================================================================
// Construcción y mostrado del overlay
// =================================================================
function buildOverlay() {
    const wrap = document.createElement("div");
    wrap.className = "o_valoracion_overlay";
    wrap.setAttribute("role", "alertdialog");
    wrap.setAttribute("aria-live", "polite");
    wrap.innerHTML = [
        '<div class="o_valoracion_overlay_inner">',
        '  <div class="o_valoracion_overlay_spinner">',
        '    <div class="spinner-border" role="status">',
        '      <span class="visually-hidden">Cargando…</span>',
        '    </div>',
        '  </div>',
        '  <h3 class="o_valoracion_overlay_title">Generando valoración…</h3>',
        '  <p class="o_valoracion_overlay_msg">' + MESSAGES[0] + '</p>',
        '  <p class="o_valoracion_overlay_hint">No cierres ni recargues esta ventana.</p>',
        '</div>',
    ].join("");
    return wrap;
}

function showOverlay() {
    if (overlayEl || active) {
        return;
    }
    overlayEl = buildOverlay();
    document.body.appendChild(overlayEl);
    active = true;
    messageIndex = 0;

    rotateTimer = setInterval(() => {
        if (!overlayEl) {
            return;
        }
        messageIndex = (messageIndex + 1) % MESSAGES.length;
        const msgEl = overlayEl.querySelector(".o_valoracion_overlay_msg");
        if (msgEl) {
            msgEl.textContent = MESSAGES[messageIndex];
        }
    }, ROTATE_MS);

    longTimer = setTimeout(() => {
        if (overlayEl) {
            const msgEl = overlayEl.querySelector(".o_valoracion_overlay_msg");
            if (msgEl) {
                msgEl.textContent = LONG_RUNNING_MSG;
            }
            if (rotateTimer) {
                clearInterval(rotateTimer);
                rotateTimer = null;
            }
        }
    }, LONG_THRESHOLD_MS);

    hardTimer = setTimeout(() => {
        hideOverlay();
    }, HARD_TIMEOUT_MS);
}

function hideOverlay() {
    if (rotateTimer) {
        clearInterval(rotateTimer);
        rotateTimer = null;
    }
    if (longTimer) {
        clearTimeout(longTimer);
        longTimer = null;
    }
    if (hardTimer) {
        clearTimeout(hardTimer);
        hardTimer = null;
    }
    active = false;
    pendingRequests = 0;
    if (overlayEl) {
        overlayEl.remove();
        overlayEl = null;
    }
}

function onSettleRequest() {
    pendingRequests = Math.max(0, pendingRequests - 1);
    setTimeout(() => {
        if (active && pendingRequests <= 0) {
            hideOverlay();
        }
    }, 400);
}

// =================================================================
// Wrap de fetch: detecta llamada target y muestra overlay
// =================================================================
const origFetch = window.fetch;
if (origFetch) {
    window.fetch = function (input, init) {
        const url = typeof input === "string"
            ? input
            : (input && input.url) || "";
        const body = init && init.body;

        if (isTargetRpc(url, body)) {
            showOverlay();
        }

        if (active) {
            pendingRequests += 1;
            return origFetch.apply(this, arguments).then(
                (resp) => {
                    onSettleRequest();
                    return resp;
                },
                (err) => {
                    onSettleRequest();
                    throw err;
                }
            );
        }

        return origFetch.apply(this, arguments);
    };
}

// =================================================================
// Wrap de XMLHttpRequest (legacy / fallback)
// =================================================================
const OrigXHR = window.XMLHttpRequest;
if (OrigXHR && OrigXHR.prototype) {
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;

    OrigXHR.prototype.open = function (method, url) {
        this._valoracionUrl = url;
        return origOpen.apply(this, arguments);
    };

    OrigXHR.prototype.send = function (body) {
        const url = this._valoracionUrl || "";

        if (isTargetRpc(url, body)) {
            showOverlay();
        }

        if (active) {
            pendingRequests += 1;
            this.addEventListener("load", onSettleRequest);
            this.addEventListener("error", onSettleRequest);
            this.addEventListener("abort", onSettleRequest);
            this.addEventListener("timeout", onSettleRequest);
        }

        return origSend.apply(this, arguments);
    };
}

// =================================================================
// Service stub para registrar el JS como parte de assets_backend
// =================================================================
const valoracionOverlayService = {
    dependencies: [],
    start() {
        // Toda la inicialización se hace a nivel de módulo (fetch/XHR wraps).
        // Este service expone solo helpers para debug manual.
        return {
            isActive: () => active,
            forceHide: () => hideOverlay(),
        };
    },
};

registry.category("services").add("valoracion_overlay", valoracionOverlayService);

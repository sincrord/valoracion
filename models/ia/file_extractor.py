# -*- coding: utf-8 -*-
"""Extractor de texto/contenido para los archivos del cliente y las fuentes.

Soporta:
  * PDF       -> texto vía pdfplumber (preferido) o PyPDF2 (fallback)
  * XLSX/XLS  -> texto vía openpyxl
  * CSV       -> texto raw con detección de encoding (chardet)
  * TXT       -> texto raw con detección de encoding (chardet)
  * Imágenes  -> NO se extrae texto local; se devuelven en base64 con su
                 media_type para que el provider las envíe como input
                 multimodal a Claude (decisión técnica: más preciso que OCR
                 local en v1).

Las dependencias externas (pdfplumber, openpyxl, pandas, chardet) están
declaradas en __manifest__.py / external_dependencies.

El extractor NO toca el ORM. Recibe bytes/base64 y devuelve dicts. La
persistencia de cache la hace el llamador (valoracion.archivo.cliente o
valoracion.fuente).
"""
import base64
import io
import logging

_logger = logging.getLogger(__name__)


# Cap por archivo, expresado en caracteres del texto extraído.
# Evita que un PDF de 500 páginas inunde el prompt.
MAX_TEXT_PER_FILE_CHARS = 200_000

# Mapa de extensión de imagen -> media type para Claude multimodal
IMAGE_MEDIA_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'bmp': 'image/bmp',
}


def _result(success, text='', image_b64=None, image_media_type=None,
            error=None, truncated=False):
    """Helper para construir el dict de retorno uniforme."""
    return {
        'success': success,
        'text': text or '',
        'image_b64': image_b64,
        'image_media_type': image_media_type,
        'error': error,
        'truncated': truncated,
    }


class FileExtractor:
    """Extractor estático. Todas las operaciones son classmethod / staticmethod."""

    # ====================================================================
    # API pública
    # ====================================================================
    @classmethod
    def extract(cls, file_data_b64, file_name, file_type):
        """Extrae el contenido de un archivo.

        Args:
            file_data_b64: bytes o str base64-encoded (como Odoo entrega
                           campos Binary).
            file_name:     nombre original del archivo (para logs/errores).
            file_type:     tipo lógico ('pdf', 'xlsx', 'xls', 'csv', 'txt',
                           'imagen').

        Returns:
            dict con claves: success, text, image_b64, image_media_type,
            error, truncated.
        """
        if not file_data_b64:
            return _result(False, error='Sin datos en el archivo')

        # Normalizar a bytes para decodificar
        if isinstance(file_data_b64, str):
            file_data_b64_bytes = file_data_b64.encode('ascii')
        else:
            file_data_b64_bytes = file_data_b64

        # Caso especial: imagen -> no decodificar, pasar como base64 string
        if file_type == 'imagen':
            ext = ''
            if file_name and '.' in file_name:
                ext = file_name.rsplit('.', 1)[-1].lower()
            media_type = IMAGE_MEDIA_TYPES.get(ext, 'image/png')
            # Para Claude API el data debe ser str ASCII (no bytes)
            try:
                b64_str = file_data_b64_bytes.decode('ascii')
            except Exception as e:
                return _result(False, error='Imagen base64 inválida: %s' % e)
            return _result(
                True, image_b64=b64_str, image_media_type=media_type,
            )

        # Resto: decodificar a bytes crudos
        try:
            raw = base64.b64decode(file_data_b64_bytes)
        except Exception as e:
            return _result(False, error='Error decodificando base64: %s' % e)

        try:
            if file_type == 'pdf':
                text = cls._extract_pdf(raw)
            elif file_type in ('xlsx', 'xls'):
                text = cls._extract_excel(raw)
            elif file_type == 'csv':
                text = cls._extract_csv(raw)
            elif file_type == 'txt':
                text = cls._extract_txt(raw)
            else:
                return _result(False, error='Tipo no soportado: %s' % file_type)
        except RuntimeError as e:
            # Falta de librería externa
            return _result(False, error=str(e))
        except Exception as e:
            _logger.exception("Error extrayendo %s (%s)", file_name, file_type)
            return _result(False, error='Error de extracción: %s' % e)

        truncated = False
        if text and len(text) > MAX_TEXT_PER_FILE_CHARS:
            text = text[:MAX_TEXT_PER_FILE_CHARS] + '\n[...TRUNCADO POR LÍMITE DE CARACTERES...]'
            truncated = True

        return _result(True, text=text or '', truncated=truncated)

    # ====================================================================
    # Extractores por formato
    # ====================================================================
    @classmethod
    def _extract_pdf(cls, raw_bytes):
        """Intenta pdfplumber; si no está, prueba PyPDF2."""
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            pdfplumber = None

        if pdfplumber is not None:
            chunks = []
            try:
                with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                    for page in pdf.pages:
                        try:
                            chunk = page.extract_text() or ''
                        except Exception:
                            chunk = ''
                        chunks.append(chunk)
                return '\n'.join(chunks)
            except Exception as e:
                _logger.warning("pdfplumber falló, intentando PyPDF2: %s", e)

        # Fallback: PyPDF2
        try:
            import PyPDF2  # type: ignore
        except ImportError:
            raise RuntimeError(
                "Falta librería para PDF. Instala: pip3 install pdfplumber "
                "(o como alternativa: pip3 install PyPDF2)"
            )

        reader = PyPDF2.PdfReader(io.BytesIO(raw_bytes))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or '')
            except Exception:
                chunks.append('')
        return '\n'.join(chunks)

    @classmethod
    def _extract_excel(cls, raw_bytes):
        """Extrae texto de un libro Excel concatenando todas las hojas."""
        try:
            import openpyxl  # type: ignore
        except ImportError:
            raise RuntimeError(
                "Falta librería para Excel. Instala: pip3 install openpyxl"
            )

        wb = openpyxl.load_workbook(
            io.BytesIO(raw_bytes), read_only=True, data_only=True,
        )
        chunks = []
        for sheet in wb.worksheets:
            chunks.append('== Hoja: %s ==' % sheet.title)
            for row in sheet.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                line = '\t'.join('' if v is None else str(v) for v in row)
                chunks.append(line)
        return '\n'.join(chunks)

    @classmethod
    def _extract_csv(cls, raw_bytes):
        """Lee CSV con detección de encoding."""
        encoding = cls._detect_encoding(raw_bytes)
        try:
            return raw_bytes.decode(encoding, errors='replace')
        except Exception:
            return raw_bytes.decode('utf-8', errors='replace')

    @classmethod
    def _extract_txt(cls, raw_bytes):
        """Lee TXT con detección de encoding."""
        encoding = cls._detect_encoding(raw_bytes)
        try:
            return raw_bytes.decode(encoding, errors='replace')
        except Exception:
            return raw_bytes.decode('utf-8', errors='replace')

    # ====================================================================
    # Helpers
    # ====================================================================
    @staticmethod
    def _detect_encoding(raw_bytes):
        """Detecta encoding usando chardet sobre los primeros 64KB."""
        try:
            import chardet  # type: ignore
        except ImportError:
            return 'utf-8'
        sample = raw_bytes[:65536]
        try:
            result = chardet.detect(sample)
            enc = result.get('encoding')
            return enc or 'utf-8'
        except Exception:
            return 'utf-8'

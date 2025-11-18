#!/usr/bin/env bash
# ------------------------------------------------------
# Instalación local de Poppler y Tesseract OCR en Windows (F:\tools)
# ------------------------------------------------------

echo "🚀 Configurando entorno OCR (Poppler + Tesseract) en F:\\tools ..."

# Ruta base en Windows
TOOLS_PATH="F:/tools"
POPPLER_PATH_WIN="F:\\tools\\poppler\\Library\\bin"
TESSERACT_PATH_WIN="F:\\tools\\tesseract\\tesseract.exe"

mkdir -p "$TOOLS_PATH"
cd "$TOOLS_PATH" || exit 1

echo "📥 Descargando Poppler..."
curl -L -o poppler.zip "https://github.com/oschwartz10612/poppler-windows/releases/download/v23.11.0-0/Release-23.11.0-0.zip"
unzip -q poppler.zip -d poppler
rm poppler.zip

echo "📥 Descargando instalador de Tesseract..."
curl -L -o tesseract-installer.exe "https://github.com/UB-Mannheim/tesseract/releases/download/v5.3.0/tesseract-ocr-w64-setup-5.3.0.exe"

echo "⚙️ Instalando Tesseract en modo silencioso..."
cmd.exe /c "tesseract-installer.exe /SILENT /DIR=F:\\tools\\tesseract"
rm tesseract-installer.exe

echo "🔧 Configurando variables locales (.env)..."
if [ -f ".env" ]; then
  sed -i '/^TESSERACT_CMD/d' .env
  sed -i '/^POPPLER_PATH/d' .env
fi

echo "TESSERACT_CMD=$TESSERACT_PATH_WIN" >> .env
echo "POPPLER_PATH=$POPPLER_PATH_WIN" >> .env

echo "✅ Poppler y Tesseract instalados correctamente en F:\\tools"
echo "🧩 Se actualizó tu .env con las rutas necesarias:"
echo "   TESSERACT_CMD=$TESSERACT_PATH_WIN"
echo "   POPPLER_PATH=$POPPLER_PATH_WIN"
echo "📦 Reinicia tu PowerShell antes de continuar."

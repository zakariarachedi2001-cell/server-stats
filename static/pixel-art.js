(() => {
  const $ = (id) => document.getElementById(id);
  const fileInput = $('pixelFile');
  const canvas = $('pixelCanvas');
  const ctx = canvas.getContext('2d');
  const upload = $('pixelDrop');
  const exportButton = $('pixelDownload');
  let selectedFile = null;
  let sourceImage = null;
  let palettes = null; // fetched from the server so preview and export share one colour table

  function message(text, error = true) {
    $('pixelMsg').textContent = text;
    $('pixelMsg').hidden = !text;
    $('pixelMsg').style.color = error ? '#ff5c5c' : 'var(--accent-green)';
  }

  async function loadPalettes() {
    if (palettes) return palettes;
    try {
      const r = await fetch('/api/pixel-art/palette');
      if (!r.ok) throw new Error('palette');
      palettes = await r.json();
    } catch {
      message('Could not load the block palette. Please reload the page.');
      palettes = null;
    }
    return palettes;
  }

  // "Redmean" distance: identical to the server's _dist().
  function dist(r, g, b, p) {
    const rm = (r + p[0]) / 2;
    const dr = r - p[0], dg = g - p[1], db = b - p[2];
    return (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db;
  }

  function nearest(r, g, b, pal) {
    let best = 0, bestD = Infinity;
    for (let i = 0; i < pal.length; i++) {
      const d = dist(r, g, b, pal[i]);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  // Maps RGBA pixel data onto the palette in place (optionally with Floyd–Steinberg dithering).
  function quantize(data, size, pal, dither) {
    const clamp = (v) => Math.min(255, Math.max(0, v));
    const buf = dither ? Float64Array.from(data) : null;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const i = (y * size + x) * 4;
        const src = dither ? buf : data;
        const r = clamp(src[i]), g = clamp(src[i + 1]), b = clamp(src[i + 2]);
        const p = pal[nearest(r, g, b, pal)];
        if (dither) {
          const er = r - p[0], eg = g - p[1], eb = b - p[2];
          for (const [dx, dy, w] of [[1, 0, 7 / 16], [-1, 1, 3 / 16], [0, 1, 5 / 16], [1, 1, 1 / 16]]) {
            const nx = x + dx, ny = y + dy;
            if (nx < 0 || nx >= size || ny >= size) continue;
            const j = (ny * size + nx) * 4;
            buf[j] += er * w; buf[j + 1] += eg * w; buf[j + 2] += eb * w;
          }
        }
        data[i] = p[0]; data[i + 1] = p[1]; data[i + 2] = p[2]; data[i + 3] = 255;
      }
    }
  }

  async function drawPreview() {
    if (!sourceImage) return;
    const all = await loadPalettes();
    if (!all) return;
    const size = Number($('pixelResolution').value);
    const family = $('pixelPalette').value;
    const pal = all[family] || all.concrete;
    const work = document.createElement('canvas');
    work.width = work.height = size;
    const wctx = work.getContext('2d', { willReadFrequently: true });
    wctx.fillStyle = '#ffffff';          // transparent areas become white, like the export
    wctx.fillRect(0, 0, size, size);
    const ratio = Math.max(size / sourceImage.width, size / sourceImage.height);
    const sw = size / ratio, sh = size / ratio;
    const sx = (sourceImage.width - sw) / 2, sy = (sourceImage.height - sh) / 2;
    wctx.imageSmoothingEnabled = true;
    wctx.imageSmoothingQuality = 'high';
    wctx.drawImage(sourceImage, sx, sy, sw, sh, 0, 0, size, size);
    const imageData = wctx.getImageData(0, 0, size, size);
    quantize(imageData.data, size, pal, $('pixelDither').checked);
    wctx.putImageData(imageData, 0, 0);
    canvas.width = canvas.height = size;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(work, 0, 0);
    $('pixelDimensions').textContent = `${size} × ${size} · ${$('pixelPalette').selectedOptions[0].text}`;
    $('pixelStats').hidden = false;
    $('pixelStatWidth').textContent = size;
    $('pixelStatHeight').textContent = size;
    $('pixelStatBlocks').textContent = (size * size * Number($('pixelDepth').value)).toLocaleString();
    exportButton.disabled = false;
  }

  function loadFile(file) {
    if (!file) return;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      message('Please choose a PNG, JPG or WEBP image.'); return;
    }
    if (file.size > 8 * 1024 * 1024) { message('Image is too large. Maximum size is 8 MB.'); return; }
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      if (sourceImage && sourceImage.src.startsWith('blob:')) URL.revokeObjectURL(sourceImage.src);
      selectedFile = file;
      sourceImage = image;
      upload.querySelector('strong').textContent = file.name;
      message('Image ready. Adjust the settings, then export your Litematica schematic.', false);
      drawPreview();
    };
    image.onerror = () => { URL.revokeObjectURL(url); message('Could not open this image.'); };
    image.src = url;
  }

  fileInput.addEventListener('change', () => loadFile(fileInput.files[0]));
  for (const id of ['pixelResolution', 'pixelPalette', 'pixelDepth', 'pixelDither']) {
    $(id).addEventListener('change', drawPreview);
  }
  upload.addEventListener('dragover', e => { e.preventDefault(); upload.classList.add('dragover'); });
  upload.addEventListener('dragleave', () => upload.classList.remove('dragover'));
  upload.addEventListener('drop', e => {
    e.preventDefault(); upload.classList.remove('dragover'); loadFile(e.dataTransfer.files[0]);
  });

  exportButton.addEventListener('click', async () => {
    if (!selectedFile) return;
    exportButton.disabled = true;
    exportButton.textContent = 'CREATING...';
    message('Building your Litematica schematic...', false);
    const form = new FormData();
    form.append('image', selectedFile);
    form.append('resolution', $('pixelResolution').value);
    form.append('palette', $('pixelPalette').value);
    form.append('depth', $('pixelDepth').value);
    form.append('dither', $('pixelDither').checked ? '1' : '0');
    try {
      const response = await fetch('/api/pixel-art/export', { method: 'POST', body: form });
      if (!response.ok) {
        let data = {};
        try { data = await response.json(); } catch {}
        throw new Error(data.error || 'Export failed. Please try again.');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'pixelcraft-art.litematic';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
      message('Your .litematic file is ready. Open it with Litematica → Schematic Manager.', false);
    } catch (error) {
      message(error.message || 'Could not export the schematic.');
    } finally {
      exportButton.disabled = !selectedFile;
      exportButton.textContent = 'Download .LITEMATIC';
    }
  });
})();

(() => {
  const $ = (id) => document.getElementById(id);
  const fileInput = $('pixelFile');
  const canvas = $('pixelCanvas');
  const ctx = canvas.getContext('2d');
  const upload = $('pixelDrop');
  const exportButton = $('pixelDownload');
  let selectedFile = null;
  let sourceImage = null;

  const COLORS = [
    [207,213,214,'white'], [142,142,134,'light_gray'], [62,68,71,'gray'],
    [8,10,15,'black'], [115,71,42,'brown'], [161,39,34,'red'],
    [224,97,0,'orange'], [241,175,21,'yellow'], [94,168,24,'lime'],
    [73,91,36,'green'], [21,137,145,'cyan'], [58,175,217,'light_blue'],
    [53,57,157,'blue'], [122,42,173,'purple'], [189,68,179,'magenta'],
    [237,141,172,'pink']
  ];

  function message(text, error = true) {
    $('pixelMsg').textContent = text;
    $('pixelMsg').hidden = !text;
    $('pixelMsg').style.color = error ? '#ff5c5c' : 'var(--accent-green)';
  }

  function nearestColor(r, g, b) {
    let best = COLORS[0], bestDistance = Infinity;
    for (const color of COLORS) {
      const distance = (r-color[0])**2 + (g-color[1])**2 + (b-color[2])**2;
      if (distance < bestDistance) { best = color; bestDistance = distance; }
    }
    return best;
  }

  function drawPreview() {
    if (!sourceImage) return;
    const size = Number($('pixelResolution').value);
    const work = document.createElement('canvas');
    work.width = work.height = size;
    const wctx = work.getContext('2d', { willReadFrequently: true });
    const ratio = Math.max(size / sourceImage.width, size / sourceImage.height);
    const sw = size / ratio, sh = size / ratio;
    const sx = (sourceImage.width - sw) / 2, sy = (sourceImage.height - sh) / 2;
    wctx.imageSmoothingEnabled = true;
    wctx.drawImage(sourceImage, sx, sy, sw, sh, 0, 0, size, size);
    const imageData = wctx.getImageData(0, 0, size, size);
    for (let i = 0; i < imageData.data.length; i += 4) {
      const color = nearestColor(imageData.data[i], imageData.data[i+1], imageData.data[i+2]);
      imageData.data[i] = color[0]; imageData.data[i+1] = color[1]; imageData.data[i+2] = color[2];
      imageData.data[i+3] = 255;
    }
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
  for (const id of ['pixelResolution', 'pixelPalette', 'pixelDepth']) $(id).addEventListener('change', drawPreview);
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

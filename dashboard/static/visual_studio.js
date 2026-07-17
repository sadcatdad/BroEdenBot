(() => {
  const root = document.querySelector("[data-visual-upload]");
  if (!root) return;

  let registry = [];
  try { registry = JSON.parse(root.dataset.registry || "[]"); } catch (_) { registry = []; }
  let genericGuidance = {};
  try { genericGuidance = JSON.parse(root.dataset.genericGuidance || "{}"); } catch (_) { genericGuidance = {}; }

  const templateSelect = root.querySelector("[data-template-select]");
  const slotSelect = root.querySelector("[data-slot-select]");
  const fileInput = root.querySelector("[data-upload-file]");
  const assetType = root.querySelector("[data-asset-type]");
  const compatibility = root.querySelector("[data-file-compatibility]");
  const previewWrap = root.querySelector("[data-crop-preview]");
  const preview = root.querySelector("[data-upload-preview]");
  const focalX = root.querySelector("[data-focal-x]");
  const focalY = root.querySelector("[data-focal-y]");
  let objectUrl = "";
  let selectedImage = null;

  const selectedTemplate = () => registry.find(item => item.template_key === templateSelect.value);
  const selectedSlot = () => {
    const template = selectedTemplate();
    return template ? template.asset_slots.find(slot => slot.key === slotSelect.value) : null;
  };

  function populateSlots() {
    const template = selectedTemplate();
    const previous = slotSelect.dataset.initial || slotSelect.value;
    slotSelect.innerHTML = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = template ? "Generic asset (no slot)" : "Choose a template first";
    slotSelect.append(empty);
    if (template) {
      template.asset_slots.forEach(slot => {
        const option = document.createElement("option");
        option.value = slot.key;
        option.textContent = slot.label;
        option.selected = slot.key === previous;
        slotSelect.append(option);
      });
    }
    slotSelect.dataset.initial = "";
    updateGuidance();
  }

  function text(selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = value;
  }

  function updateGuidance() {
    const slot = selectedSlot();
    if (slot && assetType.value !== slot.asset_type) assetType.value = slot.asset_type;
    const guidance = slot || genericGuidance[assetType.value] || genericGuidance.other;
    if (!guidance) return;
    text("[data-recommended-size]", `${guidance.recommended_width} × ${guidance.recommended_height} px`);
    text("[data-aspect-ratio]", guidance.aspect_ratio);
    text("[data-minimum-size]", `${guidance.minimum_width} × ${guidance.minimum_height} px`);
    text("[data-maximum-size]", `${guidance.maximum_width} × ${guidance.maximum_height} px`);
    text("[data-final-size]", slot ? `${guidance.recommended_width} × ${guidance.recommended_height} px` : "Retains aspect within maximum bounds");
    text("[data-safe-area]", guidance.safe_area ? `${guidance.safe_area.top} top · ${guidance.safe_area.right} right · ${guidance.safe_area.bottom} bottom · ${guidance.safe_area.left} left` : "Choose a template slot for exact geometry");
    text("[data-fit]", guidance.fit || "contain");
    text("[data-supported-files]", `${guidance.formats.join(", ")} up to ${(guidance.maximum_bytes / 1e6).toFixed(0)} MB. Transparency: ${guidance.transparency}.`);
    text("[data-guidance-description]", guidance.description || "Generic assets retain their aspect ratio. Select a template slot when the destination is known.");
    updateCompatibility();
  }

  function gcd(a, b) { return b ? gcd(b, a % b) : a; }
  function updateCompatibility() {
    const slot = selectedSlot();
    const guidance = slot || genericGuidance[assetType.value] || genericGuidance.other;
    if (!selectedImage || !guidance) return;
    const width = selectedImage.naturalWidth;
    const height = selectedImage.naturalHeight;
    const divisor = gcd(width, height);
    const ratio = `${width / divisor}:${height / divisor}`;
    const target = guidance.recommended_width / guidance.recommended_height;
    const delta = Math.abs(width / height - target) / target;
    const tooSmall = width < guidance.minimum_width || height < guidance.minimum_height;
    const messages = [
      `<strong>Uploaded file</strong><br>${width} × ${height} px · ${ratio} · ${(fileInput.files[0].size / 1e6).toFixed(2)} MB · ${fileInput.files[0].type || "detected on server"}`
    ];
    if (guidance.aspect_ratio === "varies") messages.push(`<span class="compat-good">✓ Any aspect ratio is accepted for this generic asset type</span>`);
    else if (delta <= 0.015) messages.push(`<span class="compat-good">✓ Correct aspect ratio</span>`);
    else messages.push(`<span class="compat-warning">This image is ${width} × ${height} px (${ratio}). This destination recommends ${guidance.aspect_ratio}. It can be accepted after crop acknowledgement, but content may be removed when assigned to a fixed slot. Recommended replacement: ${guidance.recommended_width} × ${guidance.recommended_height} px.</span>`);
    if (tooSmall) messages.push(`<span class="compat-warning">This image is below the ${guidance.minimum_width} × ${guidance.minimum_height} px minimum and may look soft.</span>`);
    else messages.push(`<span class="compat-good">✓ Large enough for this template</span>`);
    messages.push(slot ? `Will be normalized to ${guidance.recommended_width} × ${guidance.recommended_height} px during upload.` : "Will retain its aspect ratio within the generic maximum bounds.");
    compatibility.innerHTML = messages.join("<br>");
    compatibility.hidden = false;
    preview.style.aspectRatio = `${guidance.recommended_width} / ${guidance.recommended_height}`;
    previewWrap.hidden = false;
    updateFocalPoint();
  }

  function updateFocalPoint() {
    if (!preview) return;
    preview.style.objectPosition = `${Number(focalX.value) * 100}% ${Number(focalY.value) * 100}%`;
  }

  templateSelect.addEventListener("change", populateSlots);
  slotSelect.addEventListener("change", updateGuidance);
  assetType.addEventListener("change", updateGuidance);
  focalX.addEventListener("input", updateFocalPoint);
  focalY.addEventListener("input", updateFocalPoint);
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(file);
    preview.src = objectUrl;
    preview.onload = () => { selectedImage = preview; updateCompatibility(); };
  });
  window.addEventListener("beforeunload", () => { if (objectUrl) URL.revokeObjectURL(objectUrl); });

  slotSelect.dataset.initial = new URLSearchParams(window.location.search).get("slot_key") || "";
  populateSlots();
})();

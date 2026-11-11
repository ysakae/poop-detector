(() => {
  const management = document.getElementById("management");
  const canvas = document.getElementById("tray-preview");
  const drawing = canvas.getContext("2d");
  const message = document.getElementById("preview-message");
  const original = JSON.parse(canvas.dataset.points);
  const newRoi = document.getElementById("new-roi");
  const applyButton = document.getElementById("apply-tray-button");
  const undo = document.getElementById("undo-roi");
  const keep = document.getElementById("keep-roi");
  let image = null;
  let points = original;
  let editing = false;

  function draw() {
    if (!image) return;
    drawing.drawImage(image, 0, 0, canvas.width, canvas.height);
    drawing.beginPath();
    points.forEach(([x, y], index) => {
      const position = [x * canvas.width, y * canvas.height];
      if (index === 0) drawing.moveTo(...position);
      else drawing.lineTo(...position);
    });
    if (points.length >= 3) {
      drawing.closePath();
      drawing.fillStyle = "rgba(22, 185, 156, 0.19)";
      drawing.fill();
    }
    drawing.lineWidth = 3;
    drawing.strokeStyle = "#32e0bf";
    drawing.stroke();
    for (const [x, y] of points) {
      drawing.beginPath();
      drawing.arc(x * canvas.width, y * canvas.height, 4, 0, Math.PI * 2);
      drawing.fillStyle = "#eafff7";
      drawing.fill();
    }
    if (applyButton) {
      newRoi.value = editing ? JSON.stringify({ mode: "mask", points }) : "";
      applyButton.disabled = editing ? points.length < 3 : original.length < 3;
    }
    message.textContent = editing
      ? `${points.length}点。トレーの外周に沿って3点以上を順番に選んでください。`
      : "現在の検知範囲を表示しています。";
  }

  async function refreshPreview() {
    try {
      let response = await fetch("/live/frame", { cache: "no-store" });
      if (response.status === 503) {
        await new Promise(resolve => setTimeout(resolve, 500));
        response = await fetch("/live/frame", { cache: "no-store" });
      }
      if (!response.ok) throw new Error();
      const nextImage = await createImageBitmap(await response.blob());
      if (image) image.close();
      image = nextImage;
      canvas.width = image.width;
      canvas.height = image.height;
      draw();
    } catch {
      message.textContent = "ライブ映像を取得できません。カメラを確認し、映像を撮り直してください。";
      if (applyButton) applyButton.disabled = true;
    }
  }

  document.getElementById("preview-refresh").addEventListener("click", refreshPreview);
  document.getElementById("edit-roi")?.addEventListener("click", () => {
    if (!image) return;
    editing = true;
    points = [];
    canvas.dataset.editing = "";
    undo.hidden = false;
    keep.hidden = false;
    draw();
  });
  undo?.addEventListener("click", () => { points.pop(); draw(); });
  keep?.addEventListener("click", () => {
    editing = false;
    points = original;
    delete canvas.dataset.editing;
    undo.hidden = true;
    keep.hidden = true;
    draw();
  });
  canvas.addEventListener("click", event => {
    if (!image || !editing || points.length >= 50) return;
    const rect = canvas.getBoundingClientRect();
    points.push([
      Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    ]);
    draw();
  });

  async function refreshStatus() {
    try {
      const response = await fetch("/manage/status", { cache: "no-store" });
      if (!response.ok) return;
      const status = await response.json();
      if (status.phase !== management.dataset.phase) {
        window.location.reload();
        return;
      }
      for (const key of ["monitor", "camera", "frame_time", "tracking", "message"]) {
        document.getElementById(`manage-${key}`).textContent = status[key];
      }
    } catch {
      // 次の更新で状態を取り直す。
    }
  }

  refreshPreview();
  setInterval(refreshStatus, 2000);
})();

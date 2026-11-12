// 管理 › 範囲の登録。映像を取得し、その上に多角形を描いて送る。保存にはサーバーが預かった同じ映像を使う。
(() => {
  const root = document.getElementById("areas");
  const canvas = document.getElementById("area-preview");
  const drawing = canvas.getContext("2d");
  const message = document.getElementById("area-message");
  const pointsField = document.getElementById("area-points");
  const snapshotField = document.getElementById("area-snapshot");
  const saveButton = document.getElementById("area-save");
  const drawButton = document.getElementById("area-draw");
  const undoButton = document.getElementById("area-undo");
  /** @type {Record<string, number[][]>} */
  const existing = JSON.parse(root.dataset.existing);
  let image = null;
  let points = [];
  let editing = false;

  const kind = () => document.querySelector('input[name="kind"]:checked')?.value ?? "";

  function draw() {
    if (!image) return;
    drawing.drawImage(image, 0, 0, canvas.width, canvas.height);
    const shown = editing ? points : existing[kind()] ?? [];
    drawing.beginPath();
    shown.forEach(([x, y], index) => {
      const position = [x * canvas.width, y * canvas.height];
      if (index === 0) drawing.moveTo(...position);
      else drawing.lineTo(...position);
    });
    if (shown.length >= 3) {
      drawing.closePath();
      drawing.fillStyle = editing ? "rgba(223, 157, 25, 0.22)" : "rgba(22, 185, 156, 0.19)";
      drawing.fill();
    }
    drawing.lineWidth = 3;
    drawing.strokeStyle = editing ? "#f2c25c" : "#32e0bf";
    drawing.stroke();
    for (const [x, y] of shown) {
      drawing.beginPath();
      drawing.arc(x * canvas.width, y * canvas.height, 4, 0, Math.PI * 2);
      drawing.fillStyle = "#ffffff";
      drawing.fill();
    }
    pointsField.value = editing ? JSON.stringify(points) : "";
    saveButton.disabled = !(editing && points.length >= 3 && snapshotField.value);
    message.textContent = editing
      ? `${points.length}点。範囲の外周に沿って3点以上を順番に選んでください。`
      : shown.length ? "登録済みの範囲を表示しています。" : "まだ登録されていません。";
  }

  async function capture() {
    message.textContent = "映像を取得しています。";
    try {
      const response = await fetch("/manage/areas/frame", { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text());
      const next = await createImageBitmap(await response.blob());
      if (image) image.close();
      image = next;
      snapshotField.value = response.headers.get("X-Snapshot-Id") ?? "";
      canvas.width = image.width;
      canvas.height = image.height;
      drawButton.disabled = false;
      draw();
    } catch (error) {
      snapshotField.value = "";
      saveButton.disabled = true;
      message.textContent = error instanceof Error && error.message
        ? error.message
        : "映像を取得できません。カメラと監視を確認してください。";
    }
  }

  document.getElementById("area-capture").addEventListener("click", capture);
  drawButton.addEventListener("click", () => {
    editing = true;
    points = [];
    canvas.dataset.editing = "";
    undoButton.hidden = false;
    draw();
  });
  undoButton.addEventListener("click", () => { points = points.slice(0, -1); draw(); });
  for (const radio of document.querySelectorAll('input[name="kind"]')) {
    radio.addEventListener("change", draw);
  }
  canvas.addEventListener("click", event => {
    if (!image || !editing || points.length >= 50) return;
    const rect = canvas.getBoundingClientRect();
    points = [...points, [
      Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    ]];
    draw();
  });
})();

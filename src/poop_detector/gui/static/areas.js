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
  const names = { roi: "監視範囲と基準画像", floor: "サークルの床面", outside: "トレー外の範囲" };
  let image = null;
  let points = [];
  let editing = false;
  // 映像に重ねて見せる範囲。カードの「映像で範囲を見る」か、登録するものの選択で決まる。
  let viewing = document.querySelector('input[name="kind"]:checked')?.value ?? "roi";

  const kind = () => document.querySelector('input[name="kind"]:checked')?.value ?? "";

  /** 見せる範囲と、その説明。トレー外の範囲がなければ、代わりに見張る床面を示す。 */
  function viewed() {
    const own = existing[viewing] ?? [];
    if (viewing === "outside" && own.length < 3 && (existing.floor ?? []).length >= 3) {
      return { shown: existing.floor, note: "トレー外の範囲は未登録なので、代わりに見張るサークルの床面を表示しています。" };
    }
    return {
      shown: own,
      note: own.length ? `${names[viewing]}の登録済みの範囲を表示しています。` : `${names[viewing]}はまだ登録されていません。`,
    };
  }

  function outlinePath(shape) {
    drawing.beginPath();
    shape.forEach(([x, y], index) => {
      const position = [x * canvas.width, y * canvas.height];
      if (index === 0) drawing.moveTo(...position);
      else drawing.lineTo(...position);
    });
    if (shape.length >= 3) drawing.closePath();
  }

  function showCards() {
    for (const button of document.querySelectorAll("[data-area-show]")) {
      const selected = button.dataset.areaShow === viewing;
      button.setAttribute("aria-pressed", String(selected));
      button.closest(".area-card")?.toggleAttribute("data-selected", selected);
    }
  }

  function draw() {
    showCards();
    if (!image) return;
    drawing.drawImage(image, 0, 0, canvas.width, canvas.height);
    const view = viewed();
    const shown = editing ? points : view.shown;
    if (!editing) {
      // ほかの登録済みの範囲は、見比べられるよう点線で薄く示す。
      drawing.save();
      drawing.setLineDash([8, 6]);
      drawing.lineWidth = 2;
      drawing.strokeStyle = "rgba(255, 255, 255, 0.7)";
      for (const [other, shape] of Object.entries(existing)) {
        if (shape.length >= 3 && shape !== shown && other !== viewing) {
          outlinePath(shape);
          drawing.stroke();
        }
      }
      drawing.restore();
    }
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
      : view.note + (Object.values(existing).some(shape => shape.length >= 3 && shape !== shown) ? "点線はほかの登録済みの範囲です。" : "");
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
    radio.addEventListener("change", () => { viewing = radio.value; draw(); });
  }
  for (const button of document.querySelectorAll("[data-area-show]")) {
    button.addEventListener("click", () => {
      viewing = button.dataset.areaShow;
      // 登録できる範囲なら、登録するものも合わせて選ぶ（監視中のトレーは選べない）。
      const radio = document.querySelector(`input[name="kind"][value="${viewing}"]`);
      if (radio && !radio.disabled && !editing) radio.checked = true;
      draw();
      // 映像がまだなければ取りにいき、取れたら範囲を重ねる。
      if (!image) capture();
      canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }
  showCards();
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

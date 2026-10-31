const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- Header Clock
function tick() {
  const n = new Date();
  $("date").textContent = n.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
  $("time").textContent = n.toLocaleTimeString();
}
setInterval(tick, 1000);
tick();

// ---- State
let currentMode = "device"; // "device" or "server"
let deviceStream = null;
let currentFacingMode = "user"; // "user" or "environment"
let isDeviceProcessing = false;
let isDevicePaused = false;
let deviceTimer = null;
let lastPresent = null;

const modalEl = $("regModal");
const modal = new bootstrap.Modal(modalEl);
let isRegistering = false;

// ---- UI Helpers
function setStatus(online) {
  $("sysStatus").className = "sys-status " + (online ? "on" : "off");
  $("sysStatus").textContent = online ? "● ONLINE" : "● OFFLINE";
}

function setLiveBadge(online) {
  $("liveBadge").className = "live-badge " + (online ? "on" : "off");
  $("liveBadge").textContent = online ? "● LIVE" : "● OFFLINE";
}

function showOffline(title, desc) {
  $("offlineTitle").textContent = title || "Camera Offline";
  $("offlineDesc").textContent = desc || "Connect a camera to start attendance detection.";
  $("offline").style.display = "flex";
  setStatus(false);
  setLiveBadge(false);
}

function hideOffline() {
  $("offline").style.display = "none";
  setStatus(true);
  setLiveBadge(true);
}

function updateStats(s) {
  if (!s) return;
  $("statTotal").textContent = s.total ?? 0;
  $("statPresent").textContent = s.present ?? 0;
  $("statAbsent").textContent = s.absent ?? 0;
  $("statPercent").textContent = (s.percent ?? 0) + "%";
  $("cDetected").textContent = s.detected ?? 0;
  $("cRecognized").textContent = s.recognized ?? 0;
  $("cUnknown").textContent = s.unknown ?? 0;

  if (s.detections) renderDetections(s.detections);
  if (s.events) renderEvents(s.events);

  if (lastPresent !== null && s.present !== undefined && lastPresent !== s.present) {
    loadHistory();
    loadStudents();
  }
  if (s.present !== undefined) lastPresent = s.present;
}

function renderDetections(list) {
  if (!list || !list.length) {
    $("detections").innerHTML = '<div class="text-muted">No faces in view</div>';
    return;
  }
  $("detections").innerHTML = list.map(d => {
    if (d.type === "unknown") {
      return `<div class="det unknown"><div class="det-name">⚠ Unknown Person</div><div>Attendance not marked</div></div>`;
    }
    const icon = d.type === "recognized" ? "✓" : "⏳";
    return `<div class="det ${d.type}">
      <div class="det-name">${icon} ${esc(d.name)}</div>
      <div>ID: ${esc(d.student_id)}</div>
      <div>${esc(d.status)}</div>
      <div>Confidence: ${d.confidence}%</div>
    </div>`;
  }).join("");
}

function renderEvents(list) {
  $("events").innerHTML = list && list.length
    ? list.map(e => `<div class="ev ${e.type}"><span class="t">${e.time}</span>${esc(e.message)}</div>`).join("")
    : '<div class="text-muted">No activity yet</div>';
}

// ---- Canvas Overlay Drawer for Mobile Camera
function drawOverlay(detections, frameW, frameH) {
  const canvas = $("overlayCanvas");
  const video = $("deviceVideo");
  if (!video.videoWidth || !video.videoHeight) return;

  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!detections || !detections.length) return;

  const scaleX = canvas.width / (frameW || canvas.width);
  const scaleY = canvas.height / (frameH || canvas.height);

  detections.forEach(d => {
    if (!d.box) return;
    const [x, y, w, h] = d.box;
    const dx = x * scaleX;
    const dy = y * scaleY;
    const dw = w * scaleX;
    const dh = h * scaleY;

    const isRec = d.type === "recognized";
    const isVer = d.type === "verifying";
    const color = isRec ? "#1fa855" : isVer ? "#2f6fed" : "#d93636";

    // Bounding Box
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.strokeRect(dx, dy, dw, dh);

    // Label Badge
    const label = isRec ? `✓ ${d.name} (${d.confidence}%)` : isVer ? `⏳ ${d.name}` : `⚠ Unknown Person`;
    const subLabel = isRec ? `ID: ${d.student_id} • ${d.status}` : isVer ? `Verifying...` : `Not Marked`;

    ctx.font = "bold 14px 'Segoe UI', sans-serif";
    const textWidth = Math.max(ctx.measureText(label).width, ctx.measureText(subLabel).width) + 16;
    const tagH = 42;
    const tagY = dy - tagH > 0 ? dy - tagH : dy + dh;

    ctx.fillStyle = color;
    ctx.fillRect(dx, tagY, Math.max(dw, textWidth), tagH);

    ctx.fillStyle = "#ffffff";
    ctx.fillText(label, dx + 8, tagY + 18);
    ctx.font = "12px 'Segoe UI', sans-serif";
    ctx.fillText(subLabel, dx + 8, tagY + 34);
  });
}

// ---- Mobile / Device Camera Control
async function startDeviceCamera() {
  stopDeviceCamera();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showOffline("Camera Not Supported", "Your browser does not support camera access, or you need HTTPS.");
    return;
  }

  try {
    const constraints = {
      video: {
        facingMode: currentFacingMode,
        width: { ideal: 960 },
        height: { ideal: 540 }
      },
      audio: false
    };
    deviceStream = await navigator.mediaDevices.getUserMedia(constraints);
    const video = $("deviceVideo");
    video.srcObject = deviceStream;
    await video.play();

    $("feed").style.display = "none";
    $("deviceVideo").style.display = "block";
    $("overlayCanvas").style.display = "block";
    $("camControls").style.display = "flex";
    hideOffline();
    startDeviceProcessingLoop();
  } catch (err) {
    console.error("Camera access error:", err);
    showOffline("Camera Permission Denied", "Please allow camera access in browser settings, or switch to Server CCTV.");
  }
}

function stopDeviceCamera() {
  if (deviceTimer) {
    clearTimeout(deviceTimer);
    deviceTimer = null;
  }
  if (deviceStream) {
    deviceStream.getTracks().forEach(t => t.stop());
    deviceStream = null;
  }
  const canvas = $("overlayCanvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function startDeviceProcessingLoop() {
  if (deviceTimer) clearTimeout(deviceTimer);

  async function processNextFrame() {
    if (currentMode !== "device" || isDevicePaused || !deviceStream) return;

    // If modal registration is active, pause background attendance detection to prioritize registration
    if (isRegistering) {
      deviceTimer = setTimeout(processNextFrame, 400);
      return;
    }

    const video = $("deviceVideo");
    if (video.readyState >= 2 && !isDeviceProcessing) {
      isDeviceProcessing = true;
      try {
        const capCanvas = $("captureCanvas");
        const w = 640;
        const h = Math.round((video.videoHeight / (video.videoWidth || 1)) * 640) || 480;
        capCanvas.width = w;
        capCanvas.height = h;

        const ctx = capCanvas.getContext("2d");
        ctx.drawImage(video, 0, 0, w, h);
        const dataUrl = capCanvas.toDataURL("image/jpeg", 0.72);

        const res = await fetch("/api/process_frame", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image: dataUrl })
        });
        const data = await res.json();
        if (data.success) {
          drawOverlay(data.detections, data.frame_w, data.frame_h);
          updateStats(data);
          hideOffline();
        }
      } catch (e) {
        console.warn("Frame processing error:", e);
      } finally {
        isDeviceProcessing = false;
      }
    }

    if (currentMode === "device" && !isDevicePaused) {
      deviceTimer = setTimeout(processNextFrame, 260);
    }
  }

  processNextFrame();
}

// Flip camera handler
$("btnFlipCam").addEventListener("click", async () => {
  currentFacingMode = currentFacingMode === "user" ? "environment" : "user";
  await startDeviceCamera();
  if (modalEl.classList.contains("show")) {
    await attachModalCamera();
  }
});

// Pause / Resume camera handler
$("btnToggleCam").addEventListener("click", () => {
  isDevicePaused = !isDevicePaused;
  $("btnToggleCam").innerHTML = isDevicePaused
    ? '<i class="bi bi-play-fill"></i> Resume'
    : '<i class="bi bi-pause-fill"></i> Pause';
  if (!isDevicePaused) {
    startDeviceProcessingLoop();
  } else {
    const canvas = $("overlayCanvas");
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
});

$("btnRetryCam").addEventListener("click", () => {
  if (currentMode === "device") {
    startDeviceCamera();
  } else {
    pollServerStats();
  }
});

// ---- Switch Modes: Device vs Server CCTV
$("btnModeDevice").addEventListener("click", () => {
  if (currentMode === "device") return;
  currentMode = "device";
  $("btnModeDevice").className = "btn btn-primary";
  $("btnModeServer").className = "btn btn-outline-secondary";
  startDeviceCamera();
});

$("btnModeServer").addEventListener("click", () => {
  if (currentMode === "server") return;
  currentMode = "server";
  $("btnModeServer").className = "btn btn-primary";
  $("btnModeDevice").className = "btn btn-outline-secondary";

  stopDeviceCamera();
  $("deviceVideo").style.display = "none";
  $("overlayCanvas").style.display = "none";
  $("camControls").style.display = "none";
  $("feed").style.display = "block";
  pollServerStats();
});

// ---- Server Mode Polling
async function pollServerStats() {
  if (currentMode !== "server") return;
  try {
    const s = await (await fetch("/api/stats")).json();
    setStatus(s.camera_online);
    setLiveBadge(s.camera_online);
    $("feed").style.display = s.camera_online ? "block" : "none";
    if (!s.camera_online) {
      showOffline("Server Camera Offline", "No CCTV / webcam connected to the server.");
    } else {
      hideOffline();
    }
    updateStats(s);
  } catch (e) {
    showOffline("Server Offline", "Cannot reach the attendance server.");
  }
}
setInterval(() => {
  if (currentMode === "server") pollServerStats();
}, 1000);

// ---- History
$("fDate").value = new Date().toLocaleDateString("en-CA");

function historyParams() {
  return new URLSearchParams({ date: $("fDate").value, status: $("fStatus").value, q: $("fSearch").value.trim() });
}

async function loadHistory() {
  const p = historyParams();
  $("btnExport").href = "/api/export?" + p;
  const rows = await (await fetch("/api/history?" + p)).json();
  $("historyBody").innerHTML = rows.length ? rows.map((r, i) => `
    <tr><td>${i + 1}</td>
    <td>${r.snapshot ? `<img class="thumb" src="/attendance/${esc(r.snapshot)}">` : "-"}</td>
    <td>${esc(r.student_id)}</td><td>${esc(r.name)}</td><td>${esc(r.date)}</td><td>${esc(r.time)}</td>
    <td><span class="badge ${r.status === "Present" ? "bg-success" : "bg-danger"}">${r.status}</span></td>
    <td>${r.confidence != null ? r.confidence + "%" : "-"}</td></tr>`).join("")
    : '<tr><td colspan="8" class="text-center text-muted py-4">No records found</td></tr>';
}
["fDate", "fStatus"].forEach(id => $(id).addEventListener("change", loadHistory));
$("fSearch").addEventListener("input", loadHistory);
$("btnAll").addEventListener("click", () => { $("fDate").value = ""; loadHistory(); });

// ---- Students
async function loadStudents() {
  const rows = await (await fetch("/api/students")).json();
  $("studentBody").innerHTML = rows.length ? rows.map(r => `
    <tr><td>${esc(r.student_id)}</td><td>${esc(r.name)}</td><td>${r.samples}</td>
    <td><span class="badge ${r.present ? "bg-success" : "bg-secondary"}">${r.present ? "Present" : "Absent"}</span></td>
    <td class="text-end"><button class="btn btn-sm btn-outline-danger" onclick="delStudent('${esc(r.student_id)}')">
      <i class="bi bi-trash"></i></button></td></tr>`).join("")
    : '<tr><td colspan="5" class="text-center text-muted py-4">No students registered yet. Click "Register Student".</td></tr>';
}

async function delStudent(id) {
  if (!confirm(`Delete student ${id} and all their attendance records?`)) return;
  await fetch("/api/students/" + encodeURIComponent(id), { method: "DELETE" });
  loadStudents(); loadHistory();
}

// ---- Modal Camera Helper
async function attachModalCamera() {
  const regVid = $("regVideo");
  if (currentFacingMode === "user") {
    regVid.classList.add("mirror");
  } else {
    regVid.classList.remove("mirror");
  }

  // If device camera is already active, share the live stream directly
  if (deviceStream && deviceStream.active) {
    regVid.srcObject = deviceStream;
    try {
      await regVid.play();
      $("regCamMsg").style.display = "none";
      return;
    } catch (e) {
      console.warn("Could not play regVideo with existing deviceStream:", e);
    }
  }

  // If not active, request device camera
  try {
    await startDeviceCamera();
    if (deviceStream && deviceStream.active) {
      regVid.srcObject = deviceStream;
      await regVid.play();
      $("regCamMsg").style.display = "none";
    } else {
      $("regCamMsg").style.display = "flex";
    }
  } catch (err) {
    console.error("Camera attach error:", err);
    $("regCamMsg").style.display = "flex";
  }
}

// Modal Flip Camera Button
$("btnModalFlipCam").addEventListener("click", async () => {
  currentFacingMode = currentFacingMode === "user" ? "environment" : "user";
  await startDeviceCamera();
  await attachModalCamera();
});

// ---- Register Modal Handler
$("btnRegister").addEventListener("click", async () => {
  $("regId").value = "";
  $("regName").value = "";
  $("regErr").textContent = "";
  $("capText").textContent = "";
  $("capBar").style.width = "0%";
  $("regBtn").disabled = false;
  $("regId").disabled = false;
  $("regName").disabled = false;

  modal.show();
  await attachModalCamera();
});

modalEl.addEventListener("hidden.bs.modal", () => {
  isRegistering = false;
  const regVid = $("regVideo");
  regVid.pause();
  regVid.srcObject = null;
  // Resume main attendance detection loop
  if (currentMode === "device" && !isDevicePaused) {
    startDeviceProcessingLoop();
  }
});

$("regBtn").addEventListener("click", async () => {
  const sid = $("regId").value.trim();
  const name = $("regName").value.trim();
  $("regErr").textContent = "";

  if (!sid || !name) {
    $("regErr").textContent = "Please enter both Student ID and Full Name.";
    return;
  }

  $("regBtn").disabled = true;
  $("regId").disabled = true;
  $("regName").disabled = true;

  // Initialize registration in backend
  const initRes = await fetch("/api/register_init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ student_id: sid, name: name })
  });
  const initData = await initRes.json();
  if (!initRes.ok) {
    $("regErr").textContent = initData.error || "Failed to initialize registration.";
    $("regBtn").disabled = false;
    $("regId").disabled = false;
    $("regName").disabled = false;
    return;
  }

  isRegistering = true;
  const total = initData.total || 30;
  let sampleCount = 0;
  $("capText").textContent = "Look at the camera — capturing sample 1 of " + total + "...";

  const video = ($("regVideo").srcObject && $("regVideo").readyState >= 2) ? $("regVideo") : $("deviceVideo");
  const regCanvas = $("regCanvas");

  while (isRegistering && sampleCount < total) {
    if (!video || video.readyState < 2) {
      await new Promise(r => setTimeout(r, 200));
      continue;
    }

    regCanvas.width = 480;
    regCanvas.height = Math.round((video.videoHeight / (video.videoWidth || 1)) * 480) || 360;
    const ctx = regCanvas.getContext("2d");
    ctx.drawImage(video, 0, 0, regCanvas.width, regCanvas.height);
    const dataUrl = regCanvas.toDataURL("image/jpeg", 0.85);

    try {
      const sRes = await fetch("/api/register_sample", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: sid, index: sampleCount + 1, image: dataUrl })
      });

      let sData = null;
      try {
        sData = await sRes.json();
      } catch (parseErr) {
        // Fallback if not valid JSON
      }

      if (sRes.ok && sData && sData.ok) {
        sampleCount++;
        const pct = Math.round((sampleCount / total) * 100);
        $("capBar").style.width = pct + "%";
        $("capText").textContent = `Captured ${sampleCount}/${total} samples — slowly tilt or turn head slightly...`;
        $("regErr").textContent = "";
        await new Promise(r => setTimeout(r, 180));
      } else {
        const errorMsg = (sData && sData.error) ? sData.error : "Position your face clearly inside the oval guide.";
        $("regErr").textContent = errorMsg;
        await new Promise(r => setTimeout(r, 300));
      }
    } catch (err) {
      console.warn("Sample capture error:", err);
      $("regErr").textContent = "Connecting to server... Keep face centered in frame.";
      await new Promise(r => setTimeout(r, 450));
    }
  }

  if (sampleCount >= total) {
    $("capText").textContent = "Training facial recognition model, please wait...";
    try {
      const compRes = await fetch("/api/register_complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: sid })
      });
      const compData = await compRes.json();
      if (compRes.ok && compData.ok) {
        $("capText").textContent = `Registration complete for ${compData.name} ✓`;
        $("capBar").style.width = "100%";
        loadStudents();
        loadHistory();
        setTimeout(() => modal.hide(), 1200);
      } else {
        $("regErr").textContent = compData.error || "Training failed.";
        $("regBtn").disabled = false;
      }
    } catch (e) {
      $("regErr").textContent = "Model training error: " + e.message;
      $("regBtn").disabled = false;
    }
  }
});

// Initial boot
loadHistory();
loadStudents();
startDeviceCamera();
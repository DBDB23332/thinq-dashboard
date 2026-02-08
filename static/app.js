function isAC(dev) {
  return dev.type === "AIR_CONDITIONER";
}

function pill(text, cls) {
  const span = document.createElement("span");
  span.className = `pill ${cls}`;
  span.textContent = text;
  return span;
}

function sortDevices(devices) {
  return [...devices].sort((a, b) => {
    const ao = a.online ? 1 : 0;
    const bo = b.online ? 1 : 0;
    if (ao !== bo) return ao - bo;
    const t = (a.type || "").localeCompare(b.type || "");
    if (t !== 0) return t;
    return (a.name || "").localeCompare(b.name || "");
  });
}

function ago(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 10) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}

function summarizeState(dev) {
  if (!dev.online) return "OFFLINE";
  if (dev.summary) return dev.summary;

  const s = dev.state || {};
  if (dev.type === "AIR_CONDITIONER") {
    const op = s?.operation?.airConOperationMode ?? "—";
    const t = s?.temperature?.targetTemperature ?? "—";
    const u = s?.temperature?.unit ?? "";
    return `${op} | Target ${t}${u}`;
  }
  if (dev.type === "WASHER") {
    const s0 = Array.isArray(s) ? s[0] : s;
    const cur = s0?.runState?.currentState ?? "—";
    return cur;
  }
  return "—";
}

function render(data) {
  // 현재 열려 있는 details index 기억
  const openIndexes = new Set(
    Array.from(document.querySelectorAll("#homeList details[open]"))
      .map(d => Number(d.dataset.index))
  );

  document.getElementById("lastRefresh").textContent = data.last_refresh || "-";

  const list = document.getElementById("homeList");
  list.innerHTML = "";

  (data.homes || []).forEach((home, index) => {
    const details = document.createElement("details");
    details.dataset.index = index;

    if (openIndexes.has(index)) details.open = true;

    const summary = document.createElement("summary");
    const row = document.createElement("div");
    row.className = "home-row";

    const name = document.createElement("span");
    name.className = "home-name";
    name.textContent = home.home_name;

    const statusText = home.home_status || "OFFLINE";
    let statusClass = "offline";
    if (statusText === "ONLINE") statusClass = "online";
    else if (statusText === "PARTIAL") statusClass = "partial";

    const statusPill = pill(statusText, statusClass);

    const offlineInfo = document.createElement("span");
    offlineInfo.className = "dim";
    offlineInfo.textContent = `Offline ${home.offline_count}/${home.total_devices}`;

    const updated = document.createElement("span");
    updated.className = "dim updated-ago";
    updated.dataset.iso = home.updated_at || "";
    updated.textContent = `Updated ${ago(updated.dataset.iso)}`;

    // ▶ / ▼
    row.appendChild(document.createTextNode(details.open ? "▼ " : "▶ "));
    row.appendChild(name);
    row.appendChild(document.createTextNode(" | "));
    row.appendChild(statusPill);
    row.appendChild(document.createTextNode(" | "));
    row.appendChild(offlineInfo);
    row.appendChild(document.createTextNode(" | "));
    row.appendChild(updated);

    // Delete 버튼 (삭제할 때만 prompt로 ADMIN_KEY 받기)
    const delBtn = document.createElement("button");
    delBtn.textContent = "Delete";
    delBtn.className = "delete-btn";
    delBtn.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation(); // details toggle 방지

      if (!confirm(`"${home.home_name}" 집을 삭제할까요?`)) return;

      const adminKey = prompt("관리자 비밀번호(ADMIN_KEY)를 입력하세요:");
      if (!adminKey) return;

      const res = await fetch(`/api/admin/homes/${home.home_id}`, {
        method: "DELETE",
        headers: { "x-admin-key": adminKey.trim() }
      });

      if (!res.ok) {
        const out = await res.json().catch(() => ({}));
        alert(`삭제 실패: ${out.error || res.status}`);
        return;
      }

      // 즉시 UI 반영
      details.remove();

      // 로컬 캐시도 같이 정리(선택이지만 깔끔)
      try {
        const cached = localStorage.getItem("thinq:lastStatus");
        if (cached) {
          const obj = JSON.parse(cached);
          obj.homes = (obj.homes || []).filter(h => h.home_id !== home.home_id);
          localStorage.setItem("thinq:lastStatus", JSON.stringify(obj));
        }
      } catch (e2) {}

      // 서버 캐시 반영까지 기다릴 필요는 없지만, 안전하게 1번 당겨서 동기화
      await load({ reason: "delete" });
    };

    row.appendChild(delBtn);

    details.addEventListener("toggle", () => {
      row.firstChild.textContent = details.open ? "▼ " : "▶ ";
    });

    summary.appendChild(row);
    details.appendChild(summary);

    // Devices
    const devicesWrap = document.createElement("div");
    devicesWrap.className = "devices";

    const title = document.createElement("div");
    title.className = "devices-title";
    title.textContent = "Devices:";
    devicesWrap.appendChild(title);

    const sorted = sortDevices(home.devices || []);
    sorted.forEach(dev => {
      const d = document.createElement("div");
      d.className = "device";

      const line = document.createElement("div");
      line.className = "device-line";

      const onlineText = dev.online ? "ONLINE" : "OFFLINE";
      const stateSummary = summarizeState(dev);
      line.textContent = `- ${dev.name} (${dev.type}) | ${onlineText} | ${stateSummary}`;
      d.appendChild(line);

      const control = document.createElement("div");
      control.className = "control";
      control.innerHTML = isAC(dev)
        ? `Control: <span class="readonly">(TODO: control API)</span>`
        : `Control: <span class="readonly">(read-only)</span>`;
      d.appendChild(control);

      devicesWrap.appendChild(d);
    });

    if (home.error) {
      const err = document.createElement("div");
      err.className = "dim";
      err.textContent = `Error: ${home.error}`;
      devicesWrap.appendChild(err);
    }

    details.appendChild(devicesWrap);
    list.appendChild(details);
  });
}

async function load(opts = {}) {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    const data = await res.json();
    localStorage.setItem("thinq:lastStatus", JSON.stringify(data));
    render(data);
  } catch (e) {
    console.warn("load failed:", e);
    // 실패 시에는 캐시가 있으면 캐시라도 유지(이미 렌더되어 있을 가능성이 큼)
  }
}

document.getElementById("btnRefresh").addEventListener("click", () => load({ reason: "manual" }));

document.getElementById("btnAddHome").addEventListener("click", async () => {
  const home_name = document.getElementById("homeName").value.trim();
  const pat = document.getElementById("pat").value.trim();
  const country = document.getElementById("country").value.trim() || "KR";
  const server = document.getElementById("server").value.trim();
  const client_id = document.getElementById("clientId").value.trim();
  const adminKey = document.getElementById("adminKey").value.trim();

  const msg = document.getElementById("adminMsg");
  msg.textContent = "";

  const res = await fetch("/api/admin/homes", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(adminKey ? { "x-admin-key": adminKey } : {})
    },
    body: JSON.stringify({ home_name, pat, country, server, client_id })
  });

  const out = await res.json().catch(() => ({}));
  if (!res.ok) {
    msg.textContent = `실패: ${out.error || res.status}`;
    return;
  }
  msg.textContent = `성공! home_id=${out.home_id}`;

  // 추가는 즉시 UI에서 보고 싶으니 1번 load
  await load({ reason: "add_home" });
});

// ===== Admin gate (기존 유지) =====
const ADMIN_PASS = "1234";
function setupAdminGate() {
  const adminBtn = document.getElementById("adminBtn");
  const modal = document.getElementById("adminModal");
  const panel = document.getElementById("adminPanel");
  const input = document.getElementById("adminPassInput");
  const err = document.getElementById("adminError");
  const cancel = document.getElementById("adminCancel");
  const enter = document.getElementById("adminEnter");

  if (!adminBtn || !modal || !panel || !input || !err || !cancel || !enter) {
    console.warn("Admin gate elements missing. Check dashboard.html edits.");
    return;
  }

  adminBtn.onclick = () => {
    modal.style.display = "block";
    input.value = "";
    err.textContent = "";
    input.focus();
  };

  cancel.onclick = () => { modal.style.display = "none"; };

  enter.onclick = () => {
    if (input.value === ADMIN_PASS) {
      panel.style.display = "block";
      modal.style.display = "none";
    } else {
      err.textContent = "Wrong password";
    }
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") enter.click();
  });

  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.style.display = "none";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setupAdminGate);
} else {
  setupAdminGate();
}

// ===== 최초 화면: 캐시 즉시 렌더 =====
const cached = localStorage.getItem("thinq:lastStatus");
if (cached) {
  try { render(JSON.parse(cached)); } catch (e) {}
} else {
  // 캐시가 없으면 1번은 불러와야 화면이 나옴
  load({ reason: "first_no_cache" });
}

// ===== 자동 갱신: 서버 주기(3분)에 맞춰서만 요청 =====
// 새로고침(F5) 직후에는 "즉시 요청"을 안 하고,
// 다음 3분 경계 시점에 딱 맞춰 1번 load() 후, 이후 3분마다.
(function scheduleAlignedPolling() {
  const INTERVAL = 180000; // 3분
  const now = Date.now();
  const msUntilNext = INTERVAL - (now % INTERVAL);

  setTimeout(() => {
    load({ reason: "aligned_tick" });
    setInterval(() => load({ reason: "interval" }), INTERVAL);
  }, msUntilNext);
})();

// ===== 시간 자연스럽게 흐르게 (1초마다 텍스트만 갱신) =====
setInterval(() => {
  document.querySelectorAll(".updated-ago").forEach(el => {
    const iso = el.dataset.iso || "";
    el.textContent = `Updated ${ago(iso)}`;
  });
}, 1000);
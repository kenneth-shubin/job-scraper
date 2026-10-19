// Desktop notifications for new jobs. Polls /alerts and announces job ids it has not announced yet.
(() => {
  const STORAGE_KEY = "latamJobs.lastAnnouncedId";
  const POLL_MS = 30000;
  const supported = "Notification" in window;

  // The button arrives with the bell fragment, so it is looked up on each refresh.
  function refreshButton() {
    const button = document.getElementById("desktop-alerts");
    if (button) {
      button.hidden = !supported || Notification.permission !== "default";
    }
  }

  function announce(jobs) {
    const title = jobs.length === 1 ? `New job: ${jobs[0].title}` : `${jobs.length} new jobs`;
    const body = jobs
      .slice(0, 3)
      .map((job) => `${job.title} · ${job.company}`)
      .join("\n");
    const note = new Notification(title, { body, tag: "latam-jobs" });
    note.onclick = () => {
      window.focus();
      note.close();
    };
  }

  async function poll() {
    let data;
    try {
      const response = await fetch("/alerts", {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const type = response.headers.get("content-type") || "";
      if (!response.ok || !type.includes("application/json")) {
        return;
      }
      data = await response.json();
    } catch {
      return;
    }
    const newest = data.jobs.reduce((max, job) => Math.max(max, job.id), 0);
    const stored = localStorage.getItem(STORAGE_KEY);
    // The banner already lists jobs that were waiting before this browser first polled.
    if (stored === null) {
      localStorage.setItem(STORAGE_KEY, String(newest));
      return;
    }
    const last = Number(stored) || 0;
    const fresh = data.jobs.filter((job) => job.id > last);
    if (fresh.length === 0) {
      return;
    }
    localStorage.setItem(STORAGE_KEY, String(newest));
    if (supported && Notification.permission === "granted") {
      announce(fresh);
    }
  }

  document.addEventListener("click", async (event) => {
    if (!supported || !event.target.closest("#desktop-alerts")) {
      return;
    }
    await Notification.requestPermission();
    refreshButton();
  });
  document.body.addEventListener("htmx:afterSettle", refreshButton);

  refreshButton();
  poll();
  setInterval(poll, POLL_MS);
})();

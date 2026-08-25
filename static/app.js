const elements = {
  profileUrl: document.querySelector("#profile-url"),
  scanButton: document.querySelector("#scan-button"),
  scanStatus: document.querySelector("#scan-status"),
  gallerySection: document.querySelector("#gallery-section"),
  gallerySummary: document.querySelector("#gallery-summary"),
  galleryGrid: document.querySelector("#gallery-grid"),
  selectAllButton: document.querySelector("#select-all-button"),
  selectNoneButton: document.querySelector("#select-none-button"),
  downloadSection: document.querySelector("#download-section"),
  destination: document.querySelector("#destination"),
  browseButton: document.querySelector("#browse-button"),
  downloadButton: document.querySelector("#download-button"),
  cancelButton: document.querySelector("#cancel-button"),
  progressSection: document.querySelector("#progress-section"),
  jobState: document.querySelector("#job-state"),
  progressMessage: document.querySelector("#progress-message"),
  progressBar: document.querySelector("#progress-bar"),
  progressCount: document.querySelector("#progress-count"),
  resultList: document.querySelector("#result-list"),
  artworkTemplate: document.querySelector("#artwork-template"),
};

const state = {
  scan: null,
  jobId: null,
  events: null,
};

function setStatus(message, isError = false) {
  elements.scanStatus.textContent = message;
  elements.scanStatus.style.color = isError ? "#ffadb4" : "";
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "The local app could not complete that request.");
  }
  return body;
}

function selectedImageIds() {
  return Array.from(document.querySelectorAll(".artwork-checkbox:checked")).map(
    (checkbox) => checkbox.dataset.imageId
  );
}

function renderGallery(scan) {
  elements.galleryGrid.replaceChildren();
  elements.gallerySummary.textContent =
    scan.display_name + " (" + scan.profile_id + ") — " + scan.artworkCount + " visible artworks loaded.";

  for (const artwork of scan.artworks) {
    const card = elements.artworkTemplate.content.cloneNode(true);
    const checkbox = card.querySelector(".artwork-checkbox");
    const link = card.querySelector(".artwork-link");
    const image = card.querySelector(".artwork-image");
    const id = card.querySelector(".artwork-id");

    checkbox.dataset.imageId = artwork.image_id;
    link.href = artwork.detail_url;
    image.src = artwork.thumbnail_url || "";
    image.alt = artwork.alt || "Tensor.art artwork " + artwork.image_id;
    id.textContent = artwork.image_id;
    elements.galleryGrid.append(card);
  }
}

function renderJob(job) {
  elements.progressSection.classList.remove("hidden");
  elements.jobState.textContent = job.state;
  elements.progressMessage.textContent = job.current_message || "";
  const handled = job.completed + job.skipped + job.failed;
  const percent = job.total ? Math.round((handled / job.total) * 100) : 0;
  elements.progressBar.style.width = percent + "%";
  elements.progressCount.textContent =
    handled +
    " of " +
    job.total +
    " processed — " +
    job.completed +
    " downloaded, " +
    job.skipped +
    " skipped, " +
    job.failed +
    " failed.";
  elements.resultList.replaceChildren();
  for (const result of job.results.slice().reverse()) {
    const item = document.createElement("li");
    item.className = result.status;
    item.textContent = result.image_id + ": " + result.message;
    elements.resultList.append(item);
  }
  const active = job.state === "queued" || job.state === "running";
  elements.cancelButton.classList.toggle("hidden", !active);
  elements.downloadButton.disabled = active;
}

function startEvents(jobId) {
  if (state.events) {
    state.events.close();
  }
  state.events = new EventSource("/api/jobs/" + jobId + "/events");
  state.events.onmessage = (event) => {
    const job = JSON.parse(event.data);
    renderJob(job);
    if (job.state === "completed" || job.state === "cancelled") {
      state.events.close();
      state.events = null;
    }
  };
  state.events.onerror = () => {
    if (state.events) {
      state.events.close();
      state.events = null;
    }
  };
}

elements.scanButton.addEventListener("click", async () => {
  const profileUrl = elements.profileUrl.value.trim();
  if (!profileUrl) {
    setStatus("Paste a Tensor.art profile link first.", true);
    return;
  }
  elements.scanButton.disabled = true;
  setStatus("Loading the public gallery. This may take a moment for long profiles.");
  try {
    const scan = await request("/api/profile/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_url: profileUrl }),
    });
    state.scan = scan;
    elements.destination.value = scan.default_destination;
    renderGallery(scan);
    elements.gallerySection.classList.remove("hidden");
    elements.downloadSection.classList.remove("hidden");
    setStatus("Gallery loaded.");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    elements.scanButton.disabled = false;
  }
});

elements.selectAllButton.addEventListener("click", () => {
  document.querySelectorAll(".artwork-checkbox").forEach((checkbox) => {
    checkbox.checked = true;
  });
});

elements.selectNoneButton.addEventListener("click", () => {
  document.querySelectorAll(".artwork-checkbox").forEach((checkbox) => {
    checkbox.checked = false;
  });
});

elements.browseButton.addEventListener("click", async () => {
  elements.browseButton.disabled = true;
  try {
    const result = await request("/api/folder/choose", { method: "POST" });
    if (result.path) {
      elements.destination.value = result.path;
    }
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    elements.browseButton.disabled = false;
  }
});

elements.downloadButton.addEventListener("click", async () => {
  if (!state.scan) {
    return;
  }
  const imageIds = selectedImageIds();
  if (!imageIds.length) {
    setStatus("Select at least one artwork before downloading.", true);
    return;
  }
  const metadataMode = document.querySelector('input[name="metadata"]:checked').value;
  elements.downloadButton.disabled = true;
  try {
    const job = await request("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scan_id: state.scan.scan_id,
        image_ids: imageIds,
        destination: elements.destination.value.trim(),
        metadata_mode: metadataMode,
      }),
    });
    state.jobId = job.job_id;
    renderJob(job);
    startEvents(job.job_id);
  } catch (error) {
    setStatus(error.message, true);
    elements.downloadButton.disabled = false;
  }
});

elements.cancelButton.addEventListener("click", async () => {
  if (!state.jobId) {
    return;
  }
  elements.cancelButton.disabled = true;
  try {
    const job = await request("/api/jobs/" + state.jobId + "/cancel", { method: "POST" });
    renderJob(job);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    elements.cancelButton.disabled = false;
  }
});

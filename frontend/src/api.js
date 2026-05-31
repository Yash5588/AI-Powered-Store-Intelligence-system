import axios from "axios";

// All business logic lives in FastAPI; this client only fetches. No analytics
// are computed or hardcoded here.
export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

const client = axios.create({ baseURL: API_BASE_URL, timeout: 15000 });

// Returns { data, error }. Never throws, so the UI can render error states.
async function safe(promise) {
  try {
    const res = await promise;
    return { data: res.data, error: null };
  } catch (err) {
    const msg =
      err?.response?.data?.error?.message ||
      err?.response?.data?.detail ||
      err?.message ||
      "Request failed";
    return { data: null, error: msg, status: err?.response?.status };
  }
}

export const api = {
  health: () => safe(client.get("/health")),
  metrics: (storeId) => safe(client.get(`/stores/${storeId}/metrics`)),
  funnel: (storeId) => safe(client.get(`/stores/${storeId}/funnel`)),
  heatmap: (storeId) => safe(client.get(`/stores/${storeId}/heatmap`)),
  anomalies: (storeId) => safe(client.get(`/stores/${storeId}/anomalies`)),

  listVideos: () => safe(client.get("/videos")),
  uploadVideo: (file) => {
    const form = new FormData();
    form.append("file", file);
    return safe(
      client.post("/videos/upload", form, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 120000,
      })
    );
  },
  processVideo: (jobId, body) => safe(client.post(`/videos/${jobId}/process`, body)),
  videoStatus: (jobId) => safe(client.get(`/videos/${jobId}/status`)),
  videoEvents: (jobId, limit = 100) =>
    safe(client.get(`/videos/${jobId}/events`, { params: { limit } })),
  annotatedVideoUrl: (jobId) => `${API_BASE_URL}/videos/${jobId}/annotated-video`,
  originalVideoUrl: (jobId) => `${API_BASE_URL}/videos/${jobId}/original-video`,
  latestFrameUrl: (jobId) => `${API_BASE_URL}/videos/${jobId}/latest-frame`,

  clearEvents: () => safe(client.delete("/events/clear")),
};

export const CAMERAS = [
  "CAM_ENTRY_01",
  "CAM_FLOOR_A_01",
  "CAM_FLOOR_B_01",
  "CAM_BILLING_01",
  "CAM_STOCKROOM_01",
];

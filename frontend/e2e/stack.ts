/**
 * Shared e2e stack constants.
 *
 * Port rationale: the console dev server owns 3100 and the scratch API 8001.
 * Host port 8000 looks like the natural default (and is the frontend's
 * compiled-in fallback), but on this machine it is published to an unrelated
 * Docker container (`showcase-gateway`); :3000 is an unrelated user process
 * and :8100/:3200 belong to a parallel verification run. Hence 8001/3100.
 */
export const BACKEND_PORT = 8001;
export const FRONTEND_PORT = 3100;
export const API_BASE_URL = `http://localhost:${BACKEND_PORT}`;
export const FRONTEND_BASE_URL = `http://localhost:${FRONTEND_PORT}`;
export const API_KEY = "dev-key";
/** Relative to backend/ — resolves to backend/e2e_test.db (gitignored). */
export const SCRATCH_DATABASE_URL = "sqlite:///./e2e_test.db";

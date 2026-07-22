/**
 * Pure utility for rendering session status labels.
 */
const STATUS_MAP: Record<string, string> = {
  completed: "Completed",
  running: "Running",
  failed: "Failed",
  queued: "Queued",
  cancelled: "Cancelled",
  gave_up: "Gave up",
};

export function summarizeStatus(status?: string): string {
  if (!status) return "Idle";
  return STATUS_MAP[status] || status;
}

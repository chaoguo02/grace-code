import { apiUpload } from "./client";

export interface AttachmentResult {
  attachment_id: string;
  filename: string;
  size: number;
  path: string;
}

export async function uploadAttachment(
  sessionId: string,
  file: File,
  signal?: AbortSignal,
): Promise<AttachmentResult> {
  const form = new FormData();
  form.append("file", file);
  return apiUpload<AttachmentResult>(
    `/api/sessions/${encodeURIComponent(sessionId)}/attachments`,
    form,
    signal,
  );
}

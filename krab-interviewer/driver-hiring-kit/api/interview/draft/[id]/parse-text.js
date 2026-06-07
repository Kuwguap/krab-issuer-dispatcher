import { proxyConfig, proxyToInterviewUpstream } from "../../../lib/interviewUpstream.js";

export const config = proxyConfig;

export default function handler(req, res) {
  const id = req.query?.id;
  const draftId = Array.isArray(id) ? id[0] : id;
  return proxyToInterviewUpstream(req, res, `draft/${draftId || ""}/parse-text`);
}

import { proxyConfig, proxyToInterviewUpstream } from "../lib/interviewUpstream.js";

export const config = proxyConfig;

export default function handler(req, res) {
  return proxyToInterviewUpstream(req, res, "draft");
}

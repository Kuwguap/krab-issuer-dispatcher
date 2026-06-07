import { proxyConfig, proxyToInterviewUpstream, subpathFromQuery } from "./lib/interviewUpstream.js";

export const config = proxyConfig;

export default function handler(req, res) {
  return proxyToInterviewUpstream(req, res, subpathFromQuery(req));
}

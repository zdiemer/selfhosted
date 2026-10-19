import { createServer } from "node:http";
// Aggregate connection state only. No chat/account identifiers or message text.
const connected: Record<string, boolean> = { signal: false, whatsapp: false };
export function setConnected(transport: 'signal' | 'whatsapp', value: boolean) {
  connected[transport] = value;
}
export function renderHealth(enabled: Record<string, boolean>): string {
  return Object.entries(enabled).filter(([, value]) => value)
    .map(([transport]) => `home_gateway_connected{transport="${transport}"} ${Number(connected[transport])}`)
    .join('\n') + '\n';
}
export function startHealth(enabled: Record<string, boolean>) {
  createServer((request, response) => {
    if (request.url !== '/metrics') {
      response.writeHead(404); response.end(); return;
    }
    response.writeHead(200, {'Content-Type':'text/plain; version=0.0.4'});
    response.end(renderHealth(enabled));
  }).listen(9105, '0.0.0.0');
}

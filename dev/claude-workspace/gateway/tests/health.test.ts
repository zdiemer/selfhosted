import { expect, test } from 'bun:test';
import { renderHealth, setConnected } from '../src/health.ts';
test('only enabled transports are exported, and disconnect clears readiness', () => {
  setConnected('signal', true);
  expect(renderHealth({signal:true, whatsapp:false})).toBe('home_gateway_connected{transport="signal"} 1\n');
  setConnected('signal', false);
  expect(renderHealth({signal:true})).toContain(' 0\n');
  setConnected('whatsapp', true);
  expect(renderHealth({whatsapp:true})).toContain(' 1\n');
  setConnected('whatsapp', false);
  expect(renderHealth({whatsapp:true})).toContain(' 0\n');
});

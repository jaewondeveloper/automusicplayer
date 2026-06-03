import iconv from 'iconv-lite';
import { endpoint } from './routes.js';
import type { RouteBundle } from './types.js';

const UA = 'comsigan-api/1.0 (+https://github.com/jaewondeveloper/comsigan-api-nodejs)';

export async function fetchJson(url: string): Promise<Record<string, unknown>> {
  const res = await fetch(url, { headers: { 'User-Agent': UA } });
  const text = (await res.text()).replace(/\0/g, '');
  return JSON.parse(text) as Record<string, unknown>;
}

export function buildTimetableUrl(routes: RouteBundle, schoolCode: number, dateIndex: number): string {
  const param = Buffer.from(`${routes.timetablePrefix}${schoolCode}_0_${dateIndex}`).toString('base64');
  return `${endpoint(routes)}?${param}`;
}

export function buildSearchUrl(routes: RouteBundle, query: string): string {
  const bytes = iconv.encode(query, 'euc-kr');
  let encoded = '';
  for (const b of bytes) {
    encoded += `%${b.toString(16).padStart(2, '0').toUpperCase()}`;
  }
  return `${endpoint(routes)}?${routes.searchRoute}l${encoded}`;
}

import iconv from 'iconv-lite';
import { ParseError } from './errors.js';
import type { RouteBundle } from './types.js';

const BASE_URL = 'http://comci.net:4082';

let cache: RouteBundle | null = null;
let cacheAt = 0;
const CACHE_TTL = 300_000;

function extractOne(pattern: RegExp, text: string, label: string): string {
  const m = text.match(pattern);
  if (!m?.[1]) throw new ParseError(`Could not find ${label} in /st`);
  return m[1];
}

export function parseRoutes(stHtml: string): RouteBundle {
  const search = stHtml.match(/function school_ra\(sc\)\{\$\.ajax\(\{ url:'\.\/(\d+)\?(\d+)l'/);
  if (!search) throw new ParseError('search endpoint in /st');
  return {
    mainRoute: search[1],
    searchRoute: search[2],
    timetablePrefix: extractOne(/sc_data\('(\d+_)'/, stHtml, 'timetable prefix'),
    originalCode: extractOne(/원자료=Q자료\(자료\.자료(\d+)/, stHtml, 'original data code'),
    dailyCode: extractOne(/일일자료=Q자료\(자료\.자료(\d+)/, stHtml, 'daily data code'),
    subjectCode: extractOne(/자료\.자료(\d+)\[sb\]/, stHtml, 'subject array code'),
    teacherCode: extractOne(/자료\.자료(\d+)\[th\]/, stHtml, 'teacher array code'),
    updatedCode: extractOne(/수정일: '\+H시간표\.자료(\d+)/, stHtml, 'updated timestamp code'),
  };
}

export async function getRoutes(forceRefresh = false): Promise<RouteBundle> {
  const now = Date.now();
  if (!forceRefresh && cache && now - cacheAt < CACHE_TTL) return cache;
  const res = await fetch(`${BASE_URL}/st`);
  const buf = Buffer.from(await res.arrayBuffer());
  const html = iconv.decode(buf, 'euc-kr');
  cache = parseRoutes(html);
  cacheAt = now;
  return cache;
}

export function endpoint(routes: RouteBundle): string {
  return `${BASE_URL}/${routes.mainRoute}`;
}

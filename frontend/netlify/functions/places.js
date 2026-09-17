// netlify/functions/places.js
// Proxies Amazon Location Service Places v2 calls so the API key stays server-side.

const ALLOWED_ENDPOINTS = new Set(['autocomplete', 'geocode', 'reverse-geocode']);
const REGION = process.env.AWS_LOCATION_REGION || 'eu-north-1';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Content-Type': 'application/json',
};

exports.handler = async (event) => {
  // Preflight
  if (event.httpMethod === 'OPTIONS') {
    return { statusCode: 204, headers: CORS_HEADERS, body: '' };
  }

  if (event.httpMethod !== 'POST') {
    return {
      statusCode: 405,
      headers: CORS_HEADERS,
      body: JSON.stringify({ error: 'Method not allowed' }),
    };
  }

  const apiKey = process.env.AWS_LOCATION_API_KEY;
  if (!apiKey) {
    console.error('Missing AWS_LOCATION_API_KEY environment variable');
    return {
      statusCode: 500,
      headers: CORS_HEADERS,
      body: JSON.stringify({ error: 'Server is missing AWS_LOCATION_API_KEY' }),
    };
  }

  let payload;
  try {
    payload = JSON.parse(event.body || '{}');
  } catch {
    return {
      statusCode: 400,
      headers: CORS_HEADERS,
      body: JSON.stringify({ error: 'Invalid JSON body' }),
    };
  }

  const { endpoint, body } = payload;
  if (!endpoint || !ALLOWED_ENDPOINTS.has(endpoint)) {
    return {
      statusCode: 400,
      headers: CORS_HEADERS,
      body: JSON.stringify({ error: `Invalid endpoint: ${endpoint}` }),
    };
  }

  const url = `https://places.geo.${REGION}.amazonaws.com/v2/${endpoint}?key=${encodeURIComponent(apiKey)}`;

  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });

    const text = await upstream.text();

    if (!upstream.ok) {
      console.warn(`Amazon Places ${endpoint} -> ${upstream.status}:`, text.slice(0, 300));
    }

    return {
      statusCode: upstream.status,
      headers: CORS_HEADERS,
      body: text,
    };
  } catch (err) {
    console.error('Places proxy error:', err);
    return {
      statusCode: 502,
      headers: CORS_HEADERS,
      body: JSON.stringify({ error: 'Upstream request failed', detail: err.message }),
    };
  }
};

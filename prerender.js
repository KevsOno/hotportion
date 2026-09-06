const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8000';
const OUTPUT_DIR = './dist';

async function prerender() {
    console.log('🚀 Starting prerender...');

    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    });

    const page = await browser.newPage();

    // Wait for all JavaScript to execute and JSON‑LD to be injected
    await page.setExtraHTTPHeaders({
        'User-Agent': 'Googlebot/2.1 (+http://www.google.com/bot.html)'
    });

    // ─── 1. Homepage ───
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle2', timeout: 30000 });

    // Wait for products to render
    await page.waitForSelector('#menu-grid .menu-card', { timeout: 10000 }).catch(() => {});

    const html = await page.content();
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'index.html'), html);
    console.log('✅ Homepage prerendered → index.html');

    // ─── 2. Admin page ───
    await page.goto(`${BASE_URL}/admin.html`, { waitUntil: 'networkidle2', timeout: 30000 });
    const adminHtml = await page.content();
    fs.writeFileSync(path.join(OUTPUT_DIR, 'admin.html'), adminHtml);
    console.log('✅ Admin page prerendered → admin.html');

    // ─── 3. Generate sitemap.xml ───
    const sitemap = generateSitemap();
    fs.writeFileSync(path.join(OUTPUT_DIR, 'sitemap.xml'), sitemap);
    console.log('✅ Sitemap generated → sitemap.xml');

    // ─── 4. Generate robots.txt ───
    const robots = `
User-agent: *
Allow: /
Sitemap: https://hotportiongrill.com/sitemap.xml
    `.trim();
    fs.writeFileSync(path.join(OUTPUT_DIR, 'robots.txt'), robots);
    console.log('✅ Robots.txt generated → robots.txt');

    await browser.close();
    console.log('🎉 Prerender complete!');
}

function generateSitemap() {
    const baseUrl = 'https://hotportiongrill.com';
    const now = new Date().toISOString().split('T')[0];
    const urls = [
        { loc: '/', priority: '1.0', changefreq: 'daily' },
        { loc: '/admin.html', priority: '0.5', changefreq: 'monthly' },
        // Dynamic product URLs (if you have individual product pages)
        // { loc: '/product/1', priority: '0.8', changefreq: 'weekly' },
        // { loc: '/product/2', priority: '0.8', changefreq: 'weekly' },
    ];

    let xml = '<?xml version="1.0" encoding="UTF-8"?>\n';
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n';
    urls.forEach(u => {
        xml += `  <url>\n`;
        xml += `    <loc>${baseUrl}${u.loc}</loc>\n`;
        xml += `    <lastmod>${now}</lastmod>\n`;
        xml += `    <changefreq>${u.changefreq}</changefreq>\n`;
        xml += `    <priority>${u.priority}</priority>\n`;
        xml += `  </url>\n`;
    });
    xml += '</urlset>';
    return xml;
}

// Run
prerender().catch(console.error);

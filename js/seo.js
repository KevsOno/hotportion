// ─── SEO.js — Inject JSON‑LD schemas dynamically ───
(function() {
    'use strict';

    function createScriptTag(schema) {
        const script = document.createElement('script');
        script.type = 'application/ld+json';
        script.textContent = JSON.stringify(schema);
        document.head.appendChild(script);
    }

    // ─── 1. Organization Schema ───
    const organizationSchema = {
        "@context": "https://schema.org",
        "@type": "Restaurant",
        "@id": "https://hotportiongrill.com/#organization",
        "name": "Hot Portion Grill",
        "alternateName": "Hot Portion",
        "description": "Authentic Nigerian cuisine serving burgers, rice dishes, swallows, and traditional specialties in Ajegunle Apapa, Lagos.",
        "url": "https://hotportiongrill.com",
        "telephone": "+2347041006669",
        "email": "Hotportionfoods@gmail.com",
        "openingHours": "Mo-Su 10:00-22:00",
        "priceRange": "₦500 - ₦10000",
        "servesCuisine": "Nigerian, African, Continental",
        "menu": "https://hotportiongrill.com/#menu",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Ojo Road Aiyenero Junction, Ajegunle",
            "addressLocality": "Apapa",
            "addressRegion": "Lagos",
            "addressCountry": "Nigeria"
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": "6.4480",
            "longitude": "3.3490"
        },
        "sameAs": [
            "https://instagram.com/hotportiongrill",
            "https://facebook.com/hotportiongrill"
        ],
        "logo": "https://hotportiongrill.com/fastfood/images/logo.webp",
        "image": "https://hotportiongrill.com/fastfood/images/hero-banner.webp"
    };
    createScriptTag(organizationSchema);

    // ─── 2. LocalBusiness Schema (enhanced) ───
    const localBusinessSchema = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "@id": "https://hotportiongrill.com/#localbusiness",
        "name": "Hot Portion Grill",
        "description": "Premier Nigerian restaurant in Ajegunle Apapa offering traditional dishes, grilled meats, and fast food delivery.",
        "parentOrganization": {
            "@id": "https://hotportiongrill.com/#organization"
        },
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Ojo Road Aiyenero Junction, Ajegunle",
            "addressLocality": "Apapa",
            "addressRegion": "Lagos",
            "addressCountry": "NG"
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": "6.4480",
            "longitude": "3.3490"
        },
        "telephone": "+2347041006669",
        "priceRange": "₦500 - ₦10000",
        "openingHoursSpecification": [
            {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "opens": "10:00",
                "closes": "22:00"
            }
        ]
    };
    createScriptTag(localBusinessSchema);

    // ─── 3. BreadcrumbList Schema ───
    const breadcrumbSchema = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Home",
                "item": "https://hotportiongrill.com"
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Menu",
                "item": "https://hotportiongrill.com/#menu"
            }
        ]
    };
    createScriptTag(breadcrumbSchema);

    // ─── 4. Product Schemas for Menu Items ───
    // This runs after products are loaded from the API
    function injectProductSchemas(products) {
        if (!products || products.length === 0) return;

        const productSchemas = products.slice(0, 10).map(product => ({
            "@context": "https://schema.org",
            "@type": "Product",
            "@id": `https://hotportiongrill.com/product/${product.id}`,
            "name": product.name,
            "description": product.description || `${product.name} — a delicious meal from Hot Portion Grill.`,
            "image": product.image || `https://hotportiongrill.com/fastfood/images/default-product.webp`,
            "offers": {
                "@type": "Offer",
                "price": product.price,
                "priceCurrency": "NGN",
                "availability": product.stock > 0 ? "https://schema.org/InStock" : "https://schema.org/OutOfStock",
                "validFrom": new Date().toISOString().split('T')[0],
                "priceValidUntil": new Date(Date.now() + 31536000000).toISOString().split('T')[0],
                "seller": {
                    "@type": "Organization",
                    "name": "Hot Portion Grill"
                }
            },
            "category": product.tag,
            "brand": {
                "@type": "Brand",
                "name": "Hot Portion Grill"
            }
        }));

        productSchemas.forEach(schema => createScriptTag(schema));
    }

    // ─── 5. Expose function to be called after product load ───
    window.__injectProductSchemas = injectProductSchemas;

    // ─── 6. Featured Items Schema (for hero section) ───
    const featuredItemSchema = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Featured Meals at Hot Portion Grill",
        "description": "Popular dishes recommended by our chefs.",
        "numberOfItems": 8,
        "itemListElement": [
            // Dynamically populated; we inject after product load
        ]
    };
    // Store reference to update later
    window.__featuredItemList = featuredItemSchema;

    // Function to update featured items
    window.__updateFeaturedItems = function(products) {
        const topItems = products.slice(0, 8);
        window.__featuredItemList.itemListElement = topItems.map((p, idx) => ({
            "@type": "ListItem",
            "position": idx + 1,
            "name": p.name,
            "url": `https://hotportiongrill.com/#menu`,
            "image": p.image || `https://hotportiongrill.com/fastfood/images/default-product.webp`,
            "description": p.description || `${p.name} — a must‑try from Hot Portion Grill.`
        }));
        createScriptTag(window.__featuredItemList);
    };

    // ─── 7. FAQ Schema (common customer questions) ───
    const faqSchema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "What are your opening hours?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "We are open Monday through Sunday from 10:00 AM to 10:00 PM."
                }
            },
            {
                "@type": "Question",
                "name": "Do you offer delivery?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Yes, we offer delivery within Ajegunle Apapa and surrounding areas. Delivery fees apply based on distance."
                }
            },
            {
                "@type": "Question",
                "name": "What types of cuisine do you serve?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "We serve authentic Nigerian cuisine including Jollof rice, Egusi soup, Suya, Nkwobi, and traditional specialties."
                }
            },
            {
                "@type": "Question",
                "name": "How can I place an order?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "You can order directly through our website, call us at 0704 100 6669, or visit our restaurant at Ojo Road Aiyenero Junction."
                }
            }
        ]
    };
    createScriptTag(faqSchema);

    console.log('✅ SEO: JSON‑LD schemas injected');
})();

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .then(() => console.log('✅ Hot Portion SW registered'))
        .catch((err) => console.warn('SW registration failed:', err));
}

document.addEventListener('DOMContentLoaded', async () => {
  if (window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.CapacitorUpdater) {
    try {
      await window.Capacitor.Plugins.CapacitorUpdater.notifyAppReady();
      console.log('✅ Capgo: App ready');
    } catch (e) {
      console.error('❌ Capgo error:', e);
    }
  } else {
    console.log('ℹ️ Capgo skipped (Running in standard web browser)');
  }
});

# arr-recovery-watchdog
 This service will monitor Gluetun’s internal health endpoint. When Gluetun experiences an outage and subsequently becomes stably healthy, the service must call the official Prowlarr, Sonarr, and Radarr “Test All Indexers” APIs to clear their lingering circuit-breaker cooldowns.

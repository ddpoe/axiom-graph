// =============================================================================
// types.ts -- Shared interfaces for Cortex Viz frontend
// =============================================================================
/** Extract the primary display staleness from a StalenessEntry or fallback. */
export function displayStaleness(entry) {
    if (!entry)
        return 'unknown';
    // Show link status if own is clean/verified and link is stale
    const own = entry.own_status || 'unknown';
    const link = entry.link_status || 'VERIFIED';
    if ((own === 'VERIFIED' || own === 'VERIFIED') && link !== 'VERIFIED' && link !== 'VERIFIED') {
        return link;
    }
    return own;
}

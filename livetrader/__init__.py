"""livetrader — real-time paper-trading twin of the autotrader.

Where ``autotrader`` searches strategy space on frozen historical snapshots,
``livetrader`` runs an arena of genome-driven traders against LIVE market
data — current quotes, current news, current macro variables — simulating
every fill. Nothing here ever sends a real order.
"""

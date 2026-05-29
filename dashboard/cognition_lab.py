# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   dashboard/cognition_lab.py — Separate Cognitive Research Dashboard
#
#   Provides dedicated insights into:
#   - Agent observations
#   - Hypothesis tracking
#   - Prediction accuracy
#   - Anomaly detection
#   - Market regime analysis
#   - Pattern evolution
#
#   Keeps main dashboard execution-focused and lightweight.
# ============================================================

from flask import Blueprint, jsonify, render_template_string
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Create blueprint
cognition_lab = Blueprint('cognition_lab', __name__, url_prefix='/cognition')

# ════════════════════════════════════════════════════════════
#   API ENDPOINTS FOR COGNITION DATA
# ════════════════════════════════════════════════════════════

@cognition_lab.route('/status', methods=['GET'])
def cognition_status():
    """Get current cognition system status."""
    try:
        from reflection.cognition_engine import (
            load_today_cognition_cycles,
            get_unresolved_hypotheses,
            get_today_prediction_reviews,
        )
        from reflection.cognition_llm_client import get_llm_status
        
        cycles = load_today_cognition_cycles() or []
        hypotheses = get_unresolved_hypotheses() or []
        reviews = get_today_prediction_reviews() or []
        llm_status = get_llm_status()
        
        success_reviews = len([r for r in reviews if isinstance(r, dict) and r.get('result') == 'success'])
        total_reviews = len([r for r in reviews if isinstance(r, dict)])
        
        return jsonify({
            "status": "active",
            "timestamp": datetime.now().isoformat(),
            "cognition_cycles_today": len(cycles),
            "active_hypotheses": len(hypotheses),
            "prediction_reviews": total_reviews,
            "prediction_accuracy": f"{success_reviews}/{total_reviews}" if total_reviews > 0 else "N/A",
            "llm_provider": llm_status.get("preferred_provider", "unknown"),
            "llm_available": llm_status.get("openrouter_available") or llm_status.get("ollama_available"),
        })
    except Exception as e:
        logger.error(f"Cognition status failed: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500


@cognition_lab.route('/cycles/today', methods=['GET'])
def today_cycles():
    """Get all cognition cycles from today."""
    try:
        from reflection.cognition_engine import load_today_cognition_cycles
        
        cycles = load_today_cognition_cycles() or []
        
        return jsonify({
            "count": len(cycles),
            "cycles": [
                {
                    "timestamp": c.timestamp if hasattr(c, 'timestamp') else "",
                    "agent": c.agent if hasattr(c, 'agent') else "",
                    "cycle_num": c.cycle_num if hasattr(c, 'cycle_num') else 0,
                    "observation": c.market_observation if hasattr(c, 'market_observation') else "",
                    "predictions": len(c.predictions) if hasattr(c, 'predictions') else 0,
                    "anomalies": c.anomalies if hasattr(c, 'anomalies') else [],
                    "confidence": c.confidence_level if hasattr(c, 'confidence_level') else 0,
                }
                for c in cycles
            ]
        })
    except Exception as e:
        logger.error(f"Today cycles failed: {e}")
        return jsonify({"error": str(e), "cycles": []}), 500


@cognition_lab.route('/hypotheses', methods=['GET'])
def active_hypotheses():
    """Get all unresolved hypotheses."""
    try:
        from reflection.cognition_engine import get_unresolved_hypotheses
        
        hypotheses = get_unresolved_hypotheses() or []
        
        return jsonify({
            "count": len(hypotheses),
            "hypotheses": [
                {
                    "id": h.get("id", ""),
                    "hypothesis": h.get("hypothesis", ""),
                    "confidence": h.get("confidence", 0),
                    "status": h.get("status", "unknown"),
                    "created": h.get("created_date", ""),
                }
                for h in hypotheses if isinstance(h, dict)
            ]
        })
    except Exception as e:
        logger.error(f"Hypotheses fetch failed: {e}")
        return jsonify({"error": str(e), "hypotheses": []}), 500


@cognition_lab.route('/predictions/accuracy', methods=['GET'])
def prediction_accuracy():
    """Get prediction accuracy metrics."""
    try:
        from reflection.cognition_engine import get_today_prediction_reviews
        
        reviews = get_today_prediction_reviews() or []
        
        success_count = 0
        failure_count = 0
        unknown_count = 0
        
        for r in reviews:
            if isinstance(r, dict):
                result = r.get('result', 'unknown')
                if result == 'success':
                    success_count += 1
                elif result == 'failure':
                    failure_count += 1
                else:
                    unknown_count += 1
        
        total = success_count + failure_count + unknown_count
        accuracy_pct = (success_count / total * 100) if total > 0 else 0
        
        return jsonify({
            "total_predictions": total,
            "successful": success_count,
            "failed": failure_count,
            "unknown": unknown_count,
            "accuracy_percent": round(accuracy_pct, 1),
            "reviews": [
                {
                    "prediction_id": r.get("prediction_id", ""),
                    "result": r.get("result", "unknown"),
                    "analysis": r.get("analysis", ""),
                    "agent": r.get("agent", ""),
                }
                for r in reviews if isinstance(r, dict)
            ]
        })
    except Exception as e:
        logger.error(f"Prediction accuracy failed: {e}")
        return jsonify({"error": str(e), "accuracy_percent": 0}), 500


@cognition_lab.route('/reflection/today', methods=['GET'])
def today_reflection():
    """Get today's final reflection synthesis."""
    try:
        from reflection.cognition_engine import get_today_daily_reflection
        
        reflection = get_today_daily_reflection()
        
        if reflection:
            return jsonify({
                "date": reflection.get("reflection_date", ""),
                "summary": reflection.get("cognition_summary", ""),
                "strongest_patterns": reflection.get("strongest_patterns", []),
                "failed_assumptions": reflection.get("failed_assumptions", []),
                "regime_behavior": reflection.get("regime_behavior", ""),
                "anomalies": reflection.get("unexpected_anomalies", []),
                "watch_themes": reflection.get("next_day_watch_themes", []),
                "confidence": reflection.get("confidence_level", 0),
                "unresolved": reflection.get("unresolved_questions", []),
            })
        else:
            return jsonify({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "summary": "No reflection generated yet",
                "strongest_patterns": [],
                "failed_assumptions": [],
                "regime_behavior": "Unknown",
                "anomalies": [],
                "watch_themes": [],
                "confidence": 0,
                "unresolved": [],
            })
    except Exception as e:
        logger.error(f"Today reflection failed: {e}")
        return jsonify({"error": str(e), "summary": "Error retrieving reflection"}), 500


@cognition_lab.route('/anomalies/today', methods=['GET'])
def today_anomalies():
    """Get all anomalies detected today."""
    try:
        from reflection.cognition_engine import load_today_cognition_cycles
        
        cycles = load_today_cognition_cycles() or []
        
        anomalies = []
        for c in cycles:
            if hasattr(c, 'anomalies') and c.anomalies:
                for anom in c.anomalies:
                    anomalies.append({
                        "timestamp": c.timestamp if hasattr(c, 'timestamp') else "",
                        "agent": c.agent if hasattr(c, 'agent') else "",
                        "anomaly": anom,
                    })
        
        return jsonify({
            "count": len(anomalies),
            "anomalies": anomalies
        })
    except Exception as e:
        logger.error(f"Anomalies fetch failed: {e}")
        return jsonify({"error": str(e), "anomalies": []}), 500


@cognition_lab.route('/patterns/today', methods=['GET'])
def today_patterns():
    """Get all patterns identified today."""
    try:
        from reflection.cognition_engine import load_today_cognition_cycles
        
        cycles = load_today_cognition_cycles() or []
        
        patterns = []
        for c in cycles:
            if hasattr(c, 'potential_patterns') and c.potential_patterns:
                for pat in c.potential_patterns:
                    patterns.append({
                        "timestamp": c.timestamp if hasattr(c, 'timestamp') else "",
                        "agent": c.agent if hasattr(c, 'agent') else "",
                        "pattern": pat,
                    })
        
        return jsonify({
            "count": len(patterns),
            "patterns": patterns
        })
    except Exception as e:
        logger.error(f"Patterns fetch failed: {e}")
        return jsonify({"error": str(e), "patterns": []}), 500


@cognition_lab.route('/llm-status', methods=['GET'])
def llm_provider_status():
    """Get LLM provider availability status."""
    try:
        from reflection.cognition_llm_client import get_llm_status
        
        status = get_llm_status()
        
        return jsonify({
            "preferred": status.get("preferred_provider", "unknown"),
            "openrouter": {
                "available": status.get("openrouter_available", False),
                "configured": status.get("openrouter_available", False),
            },
            "ollama": {
                "available": status.get("ollama_available", False),
                "url": status.get("ollama_url", ""),
                "model": status.get("ollama_model", ""),
            },
            "available_providers": status.get("available_providers", []),
        })
    except Exception as e:
        logger.error(f"LLM status failed: {e}")
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════
#   WEB INTERFACE (Optional HTML Dashboard)
# ════════════════════════════════════════════════════════════

@cognition_lab.route('/', methods=['GET'])
def cognition_dashboard():
    """Cognition Lab dashboard HTML with adaptive learning widgets."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AlcoSoft Cognition Lab</title>
        <style>
            * { box-sizing: border-box; }
            body {
                font-family: 'Inter', 'Segoe UI', sans-serif;
                margin: 0;
                padding: 0;
                background: #f8f9fa;
            }
            header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            header .header-left { display: flex; align-items: center; gap: 20px; }
            header h1 { margin: 0; font-size: 28px; display: flex; align-items: center; gap: 10px; }
            header p { margin: 5px 0 0 0; opacity: 0.9; }
            header nav { display: flex; gap: 20px; }
            header nav a { color: white; text-decoration: none; font-weight: 500; padding: 8px 12px; border-radius: 4px; transition: background 0.3s; }
            header nav a:hover { background: rgba(255,255,255,0.2); }
            header nav a.active { background: rgba(255,255,255,0.3); }
            .status-dot { width: 12px; height: 12px; border-radius: 50%; background: #27ae60; }

            .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

            .section-title {
                font-size: 18px; font-weight: 600; color: #2c3e50;
                margin: 30px 0 15px 0; padding-bottom: 10px; border-bottom: 2px solid #667eea;
            }

            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-bottom: 20px; }
            .grid-wide { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; margin-bottom: 20px; }

            .card {
                background: white; padding: 20px; border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.08); transition: box-shadow 0.3s;
            }
            .card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); }
            .card h3 { margin: 0 0 15px 0; color: #2c3e50; font-size: 16px; display: flex; align-items: center; gap: 8px; }

            .metric { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #ecf0f1; }
            .metric:last-child { border-bottom: none; }
            .metric-label { color: #7f8c8d; font-size: 13px; }
            .metric-value { font-weight: 600; color: #667eea; font-size: 14px; }

            .table { width: 100%; font-size: 13px; border-collapse: collapse; }
            .table th { background: #f5f7fa; padding: 10px; text-align: left; color: #2c3e50; font-weight: 600; border-bottom: 2px solid #ecf0f1; }
            .table td { padding: 10px; border-bottom: 1px solid #ecf0f1; }
            .table tr:hover { background: #f9fafb; }

            .stat-big { font-size: 28px; font-weight: 700; color: #667eea; margin: 10px 0; }
            .stat-label { font-size: 12px; color: #7f8c8d; }

            .info-text { color: #95a5a6; font-size: 12px; font-style: italic; padding: 15px; background: #f5f7fa; border-radius: 4px; }
            .success { color: #27ae60; } .warning { color: #f39c12; } .error { color: #e74c3c; }
        </style>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body>
        <header>
            <div class="header-left">
                <div>
                    <h1><span class="status-dot"></span> 🧠 AlcoSoft Cognition Lab</h1>
                    <p>Market Research, Adaptive Learning & Pattern Analysis</p>
                </div>
            </div>
            <nav>
                <a href="/">📊 Dashboard</a>
                <a href="/cognition" class="active">🧠 Cognition Lab</a>
                <a href="/settings">⚙️ Settings</a>
            </nav>
        </header>

        <div class="container">
            <!-- COGNITION INSIGHTS (Top Row) -->
            <div class="section-title">📊 Cognition Insights</div>
            <div class="grid">
                <div class="card">
                    <h3>🧠 System Status</h3>
                    <div id="status-content">Loading...</div>
                </div>
                <div class="card">
                    <h3>🎯 Prediction Accuracy</h3>
                    <div id="accuracy-content">Loading...</div>
                </div>
                <div class="card">
                    <h3>💡 Active Hypotheses</h3>
                    <div id="hypotheses-content">Loading...</div>
                </div>
            </div>

            <!-- DAILY REFLECTION -->
            <div class="section-title">🦉 Daily Reflection</div>
            <div class="card">
                <h3>📝 Today's Summary</h3>
                <div id="reflection-content">Loading...</div>
            </div>

            <!-- ADAPTIVE LEARNING SECTION -->
            <div class="section-title">⚡ Adaptive Learning Monitoring</div>
            <div class="grid">
                <div class="card">
                    <h3>📈 Overall Win Rate</h3>
                    <div class="stat-big" id="adaptive-winrate">0.0%</div>
                    <div class="stat-label">Across all signals</div>
                </div>
                <div class="card">
                    <h3>🎛️ Market Regime</h3>
                    <div class="stat-big" id="adaptive-regime">1x</div>
                    <div class="stat-label">Current regime multiplier</div>
                </div>
                <div class="card">
                    <h3>⚙️ Adaptive Changes</h3>
                    <div class="stat-big" id="adaptive-changes">0</div>
                    <div class="stat-label">Config updates recorded</div>
                </div>
                <div class="card">
                    <h3>💪 Confidence State</h3>
                    <div class="stat-big" id="adaptive-confidence">0.00</div>
                    <div class="stat-label">Avg confidence strength</div>
                </div>
            </div>

            <!-- SIGNAL PERFORMANCE -->
            <div class="section-title">⭐ Signal Performance</div>
            <div class="card">
                <h3>📊 Signal Statistics</h3>
                <table class="table" id="signal-table">
                    <thead><tr>
                        <th>Signal</th><th>Trades</th><th>Win %</th><th>Avg RR</th><th>Multiplier</th><th>Confidence</th>
                    </tr></thead>
                    <tbody id="signal-body">
                        <tr><td colspan="6" class="info-text">📍 Waiting for real trades... Adaptive multipliers will appear after signals accumulate enough data (min 10 trades)</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- TIME WINDOW ANALYSIS -->
            <div class="section-title">⏰ Time Window Analysis</div>
            <div class="card">
                <h3>🕐 Market Time Windows</h3>
                <table class="table" id="window-table">
                    <thead><tr>
                        <th>Window</th><th>Trades</th><th>Win %</th><th>Avg P&L</th><th>Strength</th>
                    </tr></thead>
                    <tbody id="window-body">
                        <tr><td colspan="5" class="info-text">🕐 Execute trades in different time periods to build window analysis (min 20 trades per window)</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- SYMBOL BEHAVIOR -->
            <div class="section-title">🔍 Symbol Behavior</div>
            <div class="card">
                <h3>📍 Symbol Profiles</h3>
                <table class="table" id="symbol-table">
                    <thead><tr>
                        <th>Symbol</th><th>Volatility</th><th>SL Hit %</th><th>Recovery</th><th>SL Multiplier</th>
                    </tr></thead>
                    <tbody id="symbol-body">
                        <tr><td colspan="5" class="info-text">🎯 Trade multiple symbols to build behavior profiles (min 10 trades per symbol)</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- MULTIPLIER HISTORY -->
            <div class="section-title">📈 Multiplier Change History</div>
            <div class="card">
                <h3>🔄 Recent Multiplier Updates</h3>
                <table class="table" id="multiplier-table">
                    <thead><tr>
                        <th>Type</th><th>Key</th><th>Previous</th><th>New</th><th>Reason</th><th>Time</th>
                    </tr></thead>
                    <tbody id="multiplier-body">
                        <tr><td colspan="6" class="info-text">📊 Multiplier history will appear as the adaptive system updates multipliers based on trade outcomes</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- ADAPTIVE ALERTS -->
            <div class="section-title">⚠️ Adaptive Safety Alerts</div>
            <div class="card">
                <h3>🚨 System Alerts</h3>
                <div id="alerts-content">
                    <div class="info-text">✅ No adaptive alerts — system is functioning normally</div>
                </div>
            </div>
        </div>

        <script>
            async function loadData() {
                try {
                    // System Status
                    const statusRes = await fetch('/cognition/status');
                    const status = await statusRes.json();
                    document.getElementById('status-content').innerHTML = `
                        <div class="metric"><span class="metric-label">Cognition Cycles:</span><span class="metric-value">${status.cognition_cycles_today}</span></div>
                        <div class="metric"><span class="metric-label">Active Hypotheses:</span><span class="metric-value">${status.active_hypotheses}</span></div>
                        <div class="metric"><span class="metric-label">Prediction Reviews:</span><span class="metric-value">${status.prediction_reviews}</span></div>
                        <div class="metric"><span class="metric-label">LLM Provider:</span><span class="metric-value">${status.llm_provider}</span></div>
                    `;

                    // Prediction Accuracy
                    const accRes = await fetch('/cognition/predictions/accuracy');
                    const accuracy = await accRes.json();
                    document.getElementById('accuracy-content').innerHTML = `
                        <div class="metric"><span class="metric-label">Total Predictions:</span><span class="metric-value">${accuracy.total_predictions}</span></div>
                        <div class="metric"><span class="metric-label">Successful:</span><span class="metric-value success">${accuracy.successful}</span></div>
                        <div class="metric"><span class="metric-label">Failed:</span><span class="metric-value error">${accuracy.failed}</span></div>
                        <div class="metric"><span class="metric-label">Accuracy:</span><span class="metric-value">${accuracy.accuracy_percent}%</span></div>
                    `;

                    // Hypotheses
                    const hypRes = await fetch('/cognition/hypotheses');
                    const hypotheses = await hypRes.json();
                    const hypHtml = hypotheses.hypotheses.slice(0, 3).map(h =>
                        `<div class="metric"><span class="metric-label">${h.hypothesis}</span><span class="metric-value">${(h.confidence * 100).toFixed(0)}%</span></div>`
                    ).join('');
                    document.getElementById('hypotheses-content').innerHTML = hypHtml || '<p class="info-text">No active hypotheses yet</p>';

                    // Reflection
                    const refRes = await fetch('/cognition/reflection/today');
                    const reflection = await refRes.json();
                    document.getElementById('reflection-content').innerHTML = `
                        <div class="metric"><span class="metric-label">Summary:</span></div>
                        <p style="margin:10px 0; color:#2c3e50; font-size:14px;">${reflection.summary}</p>
                        <div class="metric"><span class="metric-label">Regime:</span><span class="metric-value">${reflection.regime_behavior}</span></div>
                        <div class="metric"><span class="metric-label">Confidence:</span><span class="metric-value">${(reflection.confidence * 100).toFixed(0)}%</span></div>
                        ${reflection.watch_themes.length > 0 ? `<div class="metric"><span class="metric-label">Watch:</span><span class="metric-value">${reflection.watch_themes.join(', ')}</span></div>` : ''}
                    `;

                    // Adaptive data
                    const adaptRes = await fetch('/api/adaptive');
                    const adapt = await adaptRes.json();
                    if (adapt.ok) {
                        document.getElementById('adaptive-winrate').textContent = adapt.overall_win_rate.toFixed(1) + '%';
                        document.getElementById('adaptive-regime').textContent = adapt.config_summary?.market_regime_multiplier?.toFixed(2) + 'x' || '1x';
                        document.getElementById('adaptive-changes').textContent = adapt.change_history?.length || 0;
                        document.getElementById('adaptive-confidence').textContent = adapt.average_confidence?.toFixed(2) || '0.00';

                        // Signal Performance
                        if (adapt.signals && adapt.signals.length > 0) {
                            const signalHtml = adapt.signals.map(s => `
                                <tr>
                                    <td>${s.signal_name || 'Unknown'}</td>
                                    <td>${s.total_trades || 0}</td>
                                    <td>${(s.win_rate || 0).toFixed(1)}%</td>
                                    <td>${(s.avg_reward_ratio || 0).toFixed(2)}</td>
                                    <td>${(s.multiplier || 1).toFixed(2)}</td>
                                    <td>${(s.confidence || 0).toFixed(2)}</td>
                                </tr>
                            `).join('');
                            document.getElementById('signal-body').innerHTML = signalHtml;
                        }

                        // Time Windows
                        if (adapt.time_windows && adapt.time_windows.length > 0) {
                            const windowHtml = adapt.time_windows.map(w => `
                                <tr>
                                    <td>${w.time_window || 'N/A'}</td>
                                    <td>${w.total_trades || 0}</td>
                                    <td>${(w.win_rate || 0).toFixed(1)}%</td>
                                    <td>₹${(w.avg_pnl || 0).toFixed(0)}</td>
                                    <td>${(w.strength || 0).toFixed(2)}</td>
                                </tr>
                            `).join('');
                            document.getElementById('window-body').innerHTML = windowHtml;
                        }

                        // Symbols
                        if (adapt.symbols && adapt.symbols.length > 0) {
                            const symbolHtml = adapt.symbols.map(s => `
                                <tr>
                                    <td>${s.symbol || 'Unknown'}</td>
                                    <td>${s.volatility_profile || 'N/A'}</td>
                                    <td>${(s.sl_hit_freq || 0).toFixed(1)}%</td>
                                    <td>${(s.recovery_rate || 0).toFixed(2)}</td>
                                    <td>${(s.sl_multiplier || 1).toFixed(2)}</td>
                                </tr>
                            `).join('');
                            document.getElementById('symbol-body').innerHTML = symbolHtml;
                        }

                        // Multiplier History
                        if (adapt.change_history && adapt.change_history.length > 0) {
                            const multHtml = adapt.change_history.slice(0, 10).map(m => `
                                <tr>
                                    <td>${m.multiplier_type}</td>
                                    <td>${m.multiplier_key}</td>
                                    <td>${(m.previous_value || 0).toFixed(2)}</td>
                                    <td>${(m.new_value || 0).toFixed(2)}</td>
                                    <td>${m.reason_source || 'Auto'}</td>
                                    <td>${new Date(m.timestamp).toLocaleTimeString()}</td>
                                </tr>
                            `).join('');
                            document.getElementById('multiplier-body').innerHTML = multHtml;
                        }

                        // Alerts
                        if (adapt.alerts && adapt.alerts.length > 0) {
                            const alertHtml = adapt.alerts.map(a => `
                                <div class="info-text" style="background:${a.level === 'danger' ? '#fadad4' : a.level === 'warning' ? '#fef3cd' : '#d4edda'}; color:${a.level === 'danger' ? '#c33' : a.level === 'warning' ? '#997404' : '#155724'};">
                                    ${a.message}
                                </div>
                            `).join('');
                            document.getElementById('alerts-content').innerHTML = alertHtml;
                        }
                    }
                } catch (e) {
                    console.error('Load failed:', e);
                }
            }

            loadData();
            setInterval(loadData, 10000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html)


# ════════════════════════════════════════════════════════════
#   INITIALIZATION
# ════════════════════════════════════════════════════════════

def init_cognition_lab(app):
    """Register cognition lab blueprint with Flask app."""
    app.register_blueprint(cognition_lab)
    logger.info("✅ Cognition Lab dashboard registered at /cognition")

import os
import re
import csv
import json
from datetime import datetime

def verify_historical_data():
    archive_dir = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\archive"
    files = [f for f in os.listdir(archive_dir) if os.path.isfile(os.path.join(archive_dir, f))]
    
    # 1. Format and Structure Checks
    filename_pattern = re.compile(r"^[A-Z0-9&\s\-]+_5minute\.csv$")
    expected_cols = ["date", "open", "high", "low", "close", "volume"]
    
    total_files = len(files)
    invalid_filenames = []
    invalid_columns = []
    
    # Statistics per stock
    stock_stats = {}
    zero_price_anomalies = []
    
    print(f"Starting optimized verification of {total_files} files in {archive_dir}...\n")
    
    for filename in files:
        filepath = os.path.join(archive_dir, filename)
        
        # Check filename format
        if not filename_pattern.match(filename):
            invalid_filenames.append(filename)
            symbol = filename.split("_")[0]
        else:
            symbol = filename.replace("_5minute.csv", "")
            
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                
                # Check columns
                if header != expected_cols:
                    invalid_columns.append((filename, header))
                    
                # Read data to collect stats
                row_count = 0
                first_row = None
                last_row = None
                
                prev_date_str = None
                prev_time_str = None
                prev_close = None
                
                # Interval statistics
                intervals_distribution = {}
                alignment_errors = 0
                
                # Overnight gaps statistics
                overnight_transitions = 0
                overnight_gaps_count = 0
                overnight_gap_p_diffs = [] # absolute percentage difference
                overnight_timestamp_gaps = [] # tuples of (close_time, open_time)
                
                for row in reader:
                    if not row:
                        continue
                    row_count += 1
                    
                    if first_row is None:
                        first_row = row
                    last_row = row
                    
                    dt_str = row[0] # YYYY-MM-DD HH:MM:SS
                    open_val = float(row[1])
                    close_val = float(row[4])
                    
                    # Manual slicing is extremely fast
                    date_str = dt_str[0:10]
                    time_str = dt_str[11:19]
                    
                    # Check 5-minute alignment (minute % 5 == 0 and second == 0)
                    minute = int(time_str[3:5])
                    second = int(time_str[6:8])
                    if minute % 5 != 0 or second != 0:
                        alignment_errors += 1
                        
                    # Check if close price is zero (anomaly)
                    if close_val == 0.0:
                        zero_price_anomalies.append({
                            "filename": filename,
                            "date": dt_str,
                            "open": open_val,
                            "close": close_val
                        })
                        
                    if prev_date_str is not None:
                        # Check if same day
                        if date_str == prev_date_str:
                            prev_secs = int(prev_time_str[0:2]) * 3600 + int(prev_time_str[3:5]) * 60 + int(prev_time_str[6:8])
                            curr_secs = int(time_str[0:2]) * 3600 + minute * 60 + second
                            delta = curr_secs - prev_secs
                            intervals_distribution[delta] = intervals_distribution.get(delta, 0) + 1
                        else:
                            # Overnight transition!
                            overnight_transitions += 1
                            
                            # Check price gap
                            if open_val != prev_close:
                                overnight_gaps_count += 1
                                if prev_close and prev_close != 0.0:
                                    p_diff = abs(open_val - prev_close) / prev_close * 100
                                    overnight_gap_p_diffs.append(p_diff)
                                else:
                                    # Handle zero prev_close
                                    overnight_gap_p_diffs.append(0.0)
                                
                            # Check timestamp gap
                            overnight_timestamp_gaps.append((prev_time_str, time_str))
                            
                    prev_date_str = date_str
                    prev_time_str = time_str
                    prev_close = close_val
                    
                # Calculate span using only first and last rows
                span_years = 0.0
                min_date_str = first_row[0] if first_row else None
                max_date_str = last_row[0] if last_row else None
                if min_date_str and max_date_str:
                    min_dt = datetime.strptime(min_date_str, "%Y-%m-%d %H:%M:%S")
                    max_dt = datetime.strptime(max_date_str, "%Y-%m-%d %H:%M:%S")
                    span_years = (max_dt - min_dt).days / 365.25
                    
                stock_stats[symbol] = {
                    "filename": filename,
                    "row_count": row_count,
                    "min_date": min_date_str,
                    "max_date": max_date_str,
                    "span_years": span_years,
                    "intervals_distribution": intervals_distribution,
                    "alignment_errors": alignment_errors,
                    "overnight_transitions": overnight_transitions,
                    "overnight_gaps_count": overnight_gaps_count,
                    "overnight_gap_p_diffs": overnight_gap_p_diffs,
                    "overnight_timestamp_gaps": overnight_timestamp_gaps
                }
                
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            stock_stats[symbol] = {
                "filename": filename,
                "error": str(e)
            }
            
    # Compile summary results
    # 1. Date Span Summary
    stocks_with_3_years = [s for s, stats in stock_stats.items() if stats.get("span_years", 0) >= 3.0]
    pct_with_3_years = (len(stocks_with_3_years) / total_files) * 100 if total_files > 0 else 0
    majority_has_3_years = pct_with_3_years >= 50.0
    
    # 2. Interval Summary
    all_alignment_errors = sum(stats.get("alignment_errors", 0) for stats in stock_stats.values() if "error" not in stats)
    
    total_within_day_intervals = 0
    exact_5min_intervals = 0
    other_intervals = {}
    
    for stats in stock_stats.values():
        if "error" in stats:
            continue
        for seconds, count in stats["intervals_distribution"].items():
            total_within_day_intervals += count
            if seconds == 300.0:
                exact_5min_intervals += count
            else:
                other_intervals[seconds] = other_intervals.get(seconds, 0) + count
                
    pct_exact_5min = (exact_5min_intervals / total_within_day_intervals) * 100 if total_within_day_intervals > 0 else 0
    
    # 3. Overnight Gaps Summary
    total_transitions = sum(stats.get("overnight_transitions", 0) for stats in stock_stats.values() if "error" not in stats)
    total_gaps = sum(stats.get("overnight_gaps_count", 0) for stats in stock_stats.values() if "error" not in stats)
    pct_gaps = (total_gaps / total_transitions) * 100 if total_transitions > 0 else 0
    
    # Collect all overnight transition timestamp gaps
    overnight_ts_counts = {}
    for stats in stock_stats.values():
        if "error" in stats:
            continue
        for gap in stats["overnight_timestamp_gaps"]:
            overnight_ts_counts[gap] = overnight_ts_counts.get(gap, 0) + 1
            
    # Sort transition timestamps by frequency
    sorted_ts_gaps = sorted(overnight_ts_counts.items(), key=lambda x: x[1], reverse=True)
    
    summary = {
        "total_files": total_files,
        "format_checks": {
            "invalid_filenames": invalid_filenames,
            "invalid_columns": invalid_columns
        },
        "date_span_checks": {
            "stocks_with_3_years_count": len(stocks_with_3_years),
            "pct_with_3_years": pct_with_3_years,
            "majority_has_3_years": majority_has_3_years
        },
        "interval_checks": {
            "alignment_errors": all_alignment_errors,
            "total_within_day_intervals": total_within_day_intervals,
            "exact_5min_intervals": exact_5min_intervals,
            "pct_exact_5min": pct_exact_5min,
            "other_intervals_distribution": other_intervals
        },
        "overnight_gaps_checks": {
            "total_transitions": total_transitions,
            "total_gaps": total_gaps,
            "pct_gaps": pct_gaps,
            "top_timestamp_gaps": sorted_ts_gaps[:10]
        },
        "zero_price_anomalies": {
            "count": len(zero_price_anomalies),
            "details": zero_price_anomalies[:50] # Limit to 50 details
        }
    }
    
    # Save detailed stats to agent directory
    output_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\.agents\challenger_historical_data_dump_m3_1\verification_results.json"
    with open(output_path, "w", encoding="utf-8") as out:
        serializable_stats = {}
        for s, stats in stock_stats.items():
            if "error" in stats:
                serializable_stats[s] = stats
                continue
            serializable_stats[s] = {
                "filename": stats["filename"],
                "row_count": stats["row_count"],
                "min_date": stats["min_date"],
                "max_date": stats["max_date"],
                "span_years": stats["span_years"],
                "alignment_errors": stats["alignment_errors"],
                "overnight_transitions": stats["overnight_transitions"],
                "overnight_gaps_count": stats["overnight_gaps_count"],
                "avg_gap_pct": sum(stats["overnight_gap_p_diffs"])/len(stats["overnight_gap_p_diffs"]) if stats["overnight_gap_p_diffs"] else 0.0,
                "intervals_distribution": {str(k): v for k, v in stats["intervals_distribution"].items()}
            }
        
        json.dump({
            "summary": summary,
            "stocks": serializable_stats
        }, out, indent=2)
        
    print("\nVerification Complete! Summary of findings:")
    print(f"1. Filenames check: {len(invalid_filenames)} invalid filenames.")
    print(f"2. Column names check: {len(invalid_columns)} invalid headers.")
    print(f"3. Date span check: {len(stocks_with_3_years)} / {total_files} stocks ({pct_with_3_years:.2f}%) have >= 3 years of data.")
    print(f"4. Interval check: {pct_exact_5min:.4f}% within-day intervals are exactly 5 minutes. Alignment errors: {all_alignment_errors}")
    print(f"5. Overnight gaps check: {pct_gaps:.2f}% of overnight transitions have price gaps (open != close).")
    print(f"6. Zero-price anomalies: {len(zero_price_anomalies)} rows with close price = 0.0.")
    print(f"Top overnight timestamp gaps (close -> open):")
    for gap, count in sorted_ts_gaps[:5]:
        print(f"  {gap[0]} -> {gap[1]}: {count} occurrences")
        
if __name__ == "__main__":
    verify_historical_data()

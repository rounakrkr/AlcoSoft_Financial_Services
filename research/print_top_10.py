import pandas as pd

def main():
    csv_path = r"c:\Extra Programs\Files\AlcoSoft_Financial_Services\research\short_opt_sweep_results.csv"
    df = pd.read_csv(csv_path)
    
    # Filter for SL hits < 11
    filtered_df = df[df["sl_hits"] < 11]
    
    # Sort by Net Return descending
    sorted_df = filtered_df.sort_values(by="net_return", ascending=False)
    
    # Get top 10
    top_10 = sorted_df.head(10)
    
    print("| Rank | Filter Type | Threshold | Exit Rule | Trades | Win Rate | Net Return | SL Hits |")
    print("|---|---|---|---|---|---|---|---|")
    for idx, (i, row) in enumerate(top_10.iterrows()):
        print(f"| {idx+1} | {row['filter_type']} | {row['threshold']:.3f} | {row['exit_rule']} | {int(row['trades'])} | {row['win_rate']:.2f}% | {row['net_return']:.2f}% | {int(row['sl_hits'])} |")

if __name__ == "__main__":
    main()

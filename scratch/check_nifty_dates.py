import pandas as pd
df5 = pd.read_csv("data/historical/NIFTY_5m.csv")
df1 = pd.read_csv("data/historical/NIFTY_1m.csv")
print("NIFTY_5m shape:", df5.shape)
print("NIFTY_1m shape:", df1.shape)
print("5m datetimes:", df5["datetime"].iloc[0], "to", df5["datetime"].iloc[-1])
print("1m datetimes:", df1["datetime"].iloc[0], "to", df1["datetime"].iloc[-1])
print("5m unique dates count:", len(df5["datetime"].str.split().str[0].unique()))
print("1m unique dates count:", len(df1["datetime"].str.split().str[0].unique()))

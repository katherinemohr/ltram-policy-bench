import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv("mem_profile.txt")
df["ts_s"] = (df["ts_ns"] - df["ts_ns"].iloc[0]) / 1e9

df.plot(x="ts_s", y=[c for c in df.columns if c.endswith("_kb")])
plt.xlabel("seconds")
plt.ylabel("kB")
plt.show()

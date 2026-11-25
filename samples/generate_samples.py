"""生成一套演示用 CSV（同一米制坐标系，单位：米）。

布局（100m x 100m，两图全覆盖，y 轴向上）：
  图一：左半 A、右半 B
  图二：按 50m 网格四象限 A / B / A / B
因此右上 (50-100, 50-100) 为 B/A 分歧片，右下 (50-100, 0-50) 为 B/B 一致，
左下 (0-50, 0-50) 为 A/A 一致，左上 (0-50, 50-100) 为 A/B 分歧片。
"""

import csv
import os

OUT = os.path.join(os.path.dirname(__file__), "data")


def write(name, header, rows):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    write("map1.csv", ["id", "class", "xmin", "ymin", "xmax", "ymax"], [
        ["M1-1", "A", 0, 0, 50, 100],
        ["M1-2", "B", 50, 0, 100, 100],
    ])
    write("map2.csv", ["id", "class", "xmin", "ymin", "xmax", "ymax"], [
        ["M2-1", "A", 0, 0, 50, 50],
        ["M2-2", "B", 50, 0, 100, 50],
        ["M2-3", "A", 50, 50, 100, 100],
        ["M2-4", "B", 0, 50, 50, 100],
    ])
    write("points.csv", ["id", "x", "y", "class"], [
        # 分歧片 (50-100,50-100)：图一B/图二A，两点均支持图一 -> 采用图一
        ["P1", 70, 75, "B"],
        ["P2", 85, 80, "B"],
        # 分歧片 (0-50,50-100)：图一A/图二B，无片内点 -> 待补测
        # 片边点：落在 y=50 分界上，不计入任何片
        ["P3", 25, 50, "A"],
        # 覆盖区外点
        ["P4", 150, 150, "A"],
        # 一致片内点，仅作记录
        ["P5", 25, 25, "A"],
    ])
    print("样例文件已写入", OUT)


if __name__ == "__main__":
    main()

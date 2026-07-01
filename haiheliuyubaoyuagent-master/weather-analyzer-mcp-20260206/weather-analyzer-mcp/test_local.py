"""
本地功能测试脚本
在打包分发前，先用此脚本验证所有功能是否正常
"""
import configparser
from weather_analyzer import WeatherAnalyzer


def main():
    config = configparser.ConfigParser()
    config.read('config.ini', encoding='utf-8')
    analyzer = WeatherAnalyzer(config)

    # 1. 获取最新时间
    print("=" * 60)
    print("[1/4] 获取最新可用时间")
    latest = analyzer.get_latest_time()
    if not latest:
        print("  ❌ 未找到数据，请检查 config.ini 中的 rootDir")
        return
    print(f"  ✅ 最新时间: {latest}")

    # 2. 测试 500hPa
    print("\n[2/4] 生成 500hPa（位势高度场 + 风羽图）")
    r = analyzer.generate_charts(latest, '500hPa')
    print(f"  状态: {r['status']}  消息: {r['message']}")
    for k, v in r['charts'].items():
        print(f"  ✅ {k}: {v}")

    # 3. 测试 700hPa
    print("\n[3/4] 生成 700hPa（位势高度场 + 风羽图 + 露点）")
    r = analyzer.generate_charts(latest, '700hPa')
    print(f"  状态: {r['status']}  消息: {r['message']}")
    for k, v in r['charts'].items():
        print(f"  ✅ {k}: {v}")

    # 4. 测试 850hPa
    print("\n[4/4] 生成 850hPa（位势高度场 + 风羽图 + 露点）")
    r = analyzer.generate_charts(latest, '850hPa')
    print(f"  状态: {r['status']}  消息: {r['message']}")
    for k, v in r['charts'].items():
        print(f"  ✅ {k}: {v}")

    print("\n" + "=" * 60)
    print("本地测试完成！如果全部 ✅，可以打包分发。")
    print("=" * 60)


if __name__ == "__main__":
    main()

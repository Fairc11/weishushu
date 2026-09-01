"""微博书 - 命令行入口"""
from __future__ import annotations
import argparse
import sys

def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(prog='weibo-book', description='📱 微书薯 v1.0.0 - 把微博主页变成一本可以保存的电子书', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='\n使用示例:\n  weibo-book https://weibo.com/u/1234567890\n  weibo-book https://weibo.com/用户 -n 50 --comments\n  weibo-book https://weibo.com/u/1234567890 --login\n  weibo-book https://weibo.com/u/1234567890 -f pdf\n\n  # 时间范围筛选\n  weibo-book https://weibo.com/u/XXXXX --start 2024-01-01 --end 2024-06-30\n\n  # 仅原创微博\n  weibo-book https://weibo.com/u/XXXXX --only-original\n\n  # 备份收藏\n  weibo-book https://weibo.com/u/XXXXX --favorites\n\n  # 图片清晰度\n  weibo-book https://weibo.com/u/XXXXX --image-quality original\n        ')
    parser.add_argument('url', nargs='?', help='微博主页 URL')
    parser.add_argument('-n', '--max-posts', type=int, default=0, help='最大提取条数（0=全部，默认 0）')
    parser.add_argument('-o', '--output', default='./output', help='输出目录（默认 ./output）')
    parser.add_argument('-f', '--format', nargs='+', choices=['md', 'pdf', 'html'], default=['md', 'pdf'], help='输出格式（默认 md pdf）')
    extract_group = parser.add_argument_group('提取类型')
    extract_group.add_argument('--favorites', action='store_true', help='提取收藏微博（而非用户发布的微博）')
    date_group = parser.add_argument_group('时间筛选')
    date_group.add_argument('--start', '--start-date', dest='start_date', help='起始日期 YYYY-MM-DD')
    date_group.add_argument('--end', '--end-date', dest='end_date', help='结束日期 YYYY-MM-DD')
    filter_group = parser.add_argument_group('内容筛选')
    filter_group.add_argument('--only-original', action='store_true', help='仅备份原创微博（不含转发）')
    comment_group = parser.add_argument_group('评论')
    comment_group.add_argument('--comments', action='store_true', help='提取评论')
    comment_group.add_argument('--comments-count', type=int, default=5, help='提取评论条数（默认 5）')
    comment_group.add_argument('--comments-type', choices=['hot', 'blogger', 'all'], default='hot', help='评论类型：hot=热评 blogger=仅博主 all=全部（默认 hot）')
    media_group = parser.add_argument_group('媒体')
    media_group.add_argument('--no-media', action='store_true', help='不下载媒体文件')
    media_group.add_argument('--image-quality', choices=['thumb180', 'mw690', 'mw1024', 'large', 'original'], default='large', help='图片清晰度（默认 large 原图）')
    login_group = parser.add_argument_group('登录')
    login_group.add_argument('--login', action='store_true', help='启动扫码登录')
    login_group.add_argument('--cookie', help='直接提供微博 Cookie 字符串')
    login_group.add_argument('--cookie-file', help='Cookie 持久化文件路径')
    # v1.1.2 起砍 no_login 模式：缓存有 cookie 就用，没有就强制扫码；CLI 不再保留 --no-login
    parser.add_argument('--version', action='store_true', help='显示版本号')
    args = parser.parse_args()
    if args.version:
        from . import __version__
        print(f'weibo-book v{__version__}')
        return
    if not args.url:
        parser.error('the following arguments are required: url')
    from .api import WeiboBook
    from .models import ExtractType, ImageQuality
    quality_map = {'thumb180': ImageQuality.THUMB, 'mw690': ImageQuality.MEDIUM, 'mw1024': ImageQuality.LARGE, 'large': ImageQuality.ORIGINAL, 'original': ImageQuality.HQ}
    book = WeiboBook(cookie_str=args.cookie, cookie_file=args.cookie_file, image_quality=quality_map.get(args.image_quality, ImageQuality.ORIGINAL))
    extract_type = ExtractType.FAVORITES if args.favorites else ExtractType.POSTS
    try:
        result = book.generate(url=args.url, max_posts=args.max_posts, output_dir=args.output, formats=args.format, comments=args.comments, comments_count=args.comments_count, comments_type=args.comments_type, download_media=not args.no_media, login=args.login, start_date=args.start_date, end_date=args.end_date, only_original=args.only_original, extract_type=extract_type)
    except KeyboardInterrupt:
        print('\n\n⚠️  已取消')
        sys.exit(1)
    except Exception as e:
        print(f'\n❌ 错误: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
if __name__ == '__main__':
    main()
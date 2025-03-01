import argparse
import logging
import os
import gc
import time
import multiprocessing
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Tuple, Dict, Callable, Iterable
import psutil

import cv2
from fuzzywuzzy import fuzz
from tqdm import tqdm

# Concurrent futures
from concurrent.futures import ProcessPoolExecutor

os.environ['OMP_NUM_THREADS'] = '1'

# ***** hardcode config *****
# 字幕最长显示秒数
subtitle_max_show_second = 10
# 字幕相似度阈值(大于此阈值判定为相似)
text_similar_threshold = 70

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)


@contextmanager
def capture_video(video_path: str) -> Callable:
    vc = cv2.VideoCapture(video_path)
    if not vc.isOpened():
        raise IOError(f'Can not open video {video_path}')
    try:
        yield vc
    finally:
        vc.release()


def get_one_frame(video_path: str, pos: int):
    with capture_video(video_path) as vc:
        vc.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = vc.read()
        if not ret or frame is None:
            raise AttributeError(f'read frame error. POS:{pos}')
    return frame


# 接受一个帧索引迭代器,返回对应的每一帧画面
def get_video_frames(video_path: str, frame_idx_iterator: Iterable = None) -> Iterable:
    if frame_idx_iterator and (not isinstance(frame_idx_iterator, Iterable)):
        raise AttributeError("frame_idx_iterator must be Iterable")

    with capture_video(video_path) as vc:
        if frame_idx_iterator is None:
            idx = 0
            while True:
                ret, frame = vc.read()
                if not ret or frame is None:
                    return
                yield idx, frame
                idx += 1
        else:
            it = iter(frame_idx_iterator)
            try:
                idx = next(it)
                target = idx
                vc.set(cv2.CAP_PROP_POS_FRAMES, idx)
                while True:
                    if idx < target:
                        ret = vc.grab()
                        if not ret:
                            return
                        idx += 1
                    else:
                        ret, frame = vc.retrieve()
                        if not ret or frame is None:
                            return
                        yield idx, frame
                        target = next(it)
            except StopIteration:
                return


def convert_time_to_frame_idx(time_str: str, fps: int) -> int:
    if not time_str:
        return 0

    t = [float(i) for i in time_str.split(':')]
    if len(t) == 3:
        td = timedelta(hours=t[0], minutes=t[1], seconds=t[2])
    elif len(t) == 2:
        td = timedelta(minutes=t[0], seconds=t[1])
    else:
        raise ValueError(f'Time data "{time_str}" does not match format "%H:%M:%S"')
    index = int(td.total_seconds() * fps)
    return index


@dataclass(frozen=True)
class OcrResult:
    box: List[List[int]]
    text: str
    score: int


@dataclass(frozen=True)
class Subtitle(OcrResult):
    frame_idx: int

    def __le__(self, other):
        return self.frame_idx <= other.frame_idx

    def __str__(self):
        return f'({self.frame_idx}){self.text}'

    __repr__ = __str__


class SubtitleOption:
    """字幕操作"""
    threshold: int = text_similar_threshold

    @classmethod
    def order_by_frame(cls, subtitles: List[Subtitle]) -> List[List[Subtitle]]:
        keys: List[int] = []
        sub_dict: Dict[int, List[Subtitle]] = dict()
        group: List[List[Subtitle]] = []

        for sub in subtitles:
            if sub_dict.get(sub.frame_idx):
                sub_dict[sub.frame_idx].append(sub)
            else:
                sub_dict[sub.frame_idx] = [sub]
            keys.append(sub.frame_idx)

        for key in sorted(keys):
            group.append(sub_dict[key])

        return group

    @classmethod
    def removed_similar(cls, sub_order_by_frame: List[List[Subtitle]]) -> List[List[Subtitle]]:
        res = [sub_order_by_frame[0]]
        for subs in sub_order_by_frame[1:]:
            if not cls.subtitles_similar(subs, res[-1]):
                res.append(subs)
            else:
                res[-1] = cls.choose_better(subs, res[-1])
        return res

    @classmethod
    def clean(cls, subtitles: List[Subtitle]) -> List[List[Subtitle]]:
        if not subtitles:
            return []
        frame_subs = cls.order_by_frame(subtitles)
        frame_subs = cls.removed_similar(frame_subs)
        return frame_subs

    @classmethod
    def at_same_flame(cls, subs: List[Subtitle]) -> bool:
        if not subs:
            return True
        s = subs[0]
        for sub in subs[1:]:
            if s.frame_idx != sub.frame_idx:
                return False
        return True

    @classmethod
    def calc_avg_score(cls, subs: List[Subtitle]) -> float:
        """计算某一帧字幕的平均分"""
        score = 0
        for sub in subs:
            score += sub.score
        return score / len(subs)

    @classmethod
    def choose_better(cls, subs1: List[Subtitle], subs2: List[Subtitle]) -> List[Subtitle]:
        """选取某一帧平均分较高的字幕"""
        s1 = cls.calc_avg_score(subs1)
        s2 = cls.calc_avg_score(subs2)
        return subs1 if s1 > s2 else subs2

    @classmethod
    def subtitles_similar(cls, subs1: List[Subtitle], subs2: List[Subtitle]) -> bool:
        """两帧的字幕是否相似"""
        text1 = ' '.join([sub.text for sub in subs1])
        text2 = ' '.join([sub.text for sub in subs2])
        return fuzz.ratio(text1, text2) >= cls.threshold


@dataclass(frozen=True)
class SubtitleFormatter:
    content: str
    start_time: timedelta
    end_time: timedelta

    @staticmethod
    def _time_formatter(t) -> str:
        total_seconds = t.total_seconds()
        minutes, seconds = divmod(total_seconds, 60)
        _minutes = int(minutes)
        _seconds = int(seconds)
        _milliseconds = int((seconds - _seconds) * 100)
        return '{:02d}:{:02d}.{:02d}'.format(_minutes, _seconds, _milliseconds)

    @property
    def lrc(self) -> str:
        start = self._time_formatter(self.start_time)
        end = self._time_formatter(self.end_time)
        return f'[{start}]{self.content}\n[{end}]'

    @property
    def txt(self) -> str:
        start = self._time_formatter(self.start_time)
        end = self._time_formatter(self.end_time)
        return f'{start} --> {end}\n{self.content}\n\n'


class Video:
    path: str
    frame_count: int
    fps: int
    origin_fps: float  # 真正的帧率不一定是整数
    height: int
    width: int

    def __init__(self, path: str):
        self.path = path
        with capture_video(path) as v:
            self.frame_count = int(v.get(cv2.CAP_PROP_FRAME_COUNT))
            self.origin_fps = v.get(cv2.CAP_PROP_FPS)
            self.fps = round(self.origin_fps)
            self.height = int(v.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.width = int(v.get(cv2.CAP_PROP_FRAME_WIDTH))

    def time_to_frame_idx(self, time_str: str) -> int:
        """获取某一时刻对应的帧索引"""
        return convert_time_to_frame_idx(time_str, self.fps)

    def time_to_frame_idxes(self, time_start: str, time_end: str, capture_interval: float) -> Iterable:
        """获取某一时间范围对应的帧索引集合"""
        frame_start = 0 if not time_start else self.time_to_frame_idx(time_start)
        frame_end = self.frame_count - 1 if not time_end else self.time_to_frame_idx(time_end)
        if frame_end < frame_start:
            raise ValueError('time_start is later than time_end')
        step = 1 if not capture_interval else int(capture_interval * self.fps)
        for frame_idx in range(frame_start, frame_end, step):
            yield frame_idx

    def count_frame(self, time_start: str, time_end: str, capture_interval: float) -> int:
        """获取某段时间内共计多少帧"""
        indexes = self.time_to_frame_idxes(time_start, time_end, capture_interval)
        return sum(1 for _ in indexes)

    def get_frames(self, frame_idx_iterator: Iterable = None) -> Iterable:
        """输入帧索引集合, 获取每一帧对应的画面"""
        return get_video_frames(self.path, frame_idx_iterator)

    def get_frames_by_time_range(self, time_start: str, time_end: str, capture_interval: float = 0.5) -> Iterable:
        """输入时间范围, 获取每一帧对应的画面"""
        indexes = self.time_to_frame_idxes(time_start, time_end, capture_interval)
        return self.get_frames(indexes)

    def get_frames_by_frame_range(self, frame_start: int, frame_end: int, frame_step: int) -> Iterable:
        """输入帧索引范围, 获取每一帧对应的画面"""
        frame_end = self.frame_count if not frame_end else frame_end + 1
        return self.get_frames(range(frame_start, frame_end, frame_step))

    def show_by_time_range(self, frame_handler: Callable = None, time_start: str = '', time_end: str = '',
                           capture_interval: float = 0.5, window_name: str = '') -> None:
        """输入时间范围, 展示对应的每一帧画面"""
        assert capture_interval >= 0
        return self.show_by_frame_iterator(
            frame_iterator=self.get_frames_by_time_range(time_start, time_end, capture_interval),
            frame_handler=frame_handler,
            window_name=window_name
        )

    def show_by_frame_range(self, frame_handler: Callable = None, frame_start: int = -1, frame_end: int = -1,
                            frame_interval: int = 1, window_name: str = '') -> None:
        """输入帧索引范围, 展示对应的每一帧画面"""
        assert frame_interval > 0
        frame_start = frame_start if frame_start != -1 else 0
        frame_end = frame_end if frame_end != -1 else self.frame_count - 1
        return self.show_by_frame_iterator(
            frame_iterator=self.get_frames_by_frame_range(frame_start, frame_end, frame_interval),
            frame_handler=frame_handler,
            window_name=window_name
        )

    def show_by_frame_iterator(self, frame_iterator: Iterable, frame_handler: Callable = None,
                               window_name: str = 'Show Frame') -> None:
        """输入帧索引迭代器, 展示对应的每一帧画面"""
        window_name = f'[{window_name}] press ESC/SPACE/ENTER to Exit, S to Save'
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_EXPANDED)
        for idx, frame in frame_iterator:
            if frame_handler:
                frame = frame_handler(frame, self)
            cv2.imshow(window_name, frame)
            key = cv2.waitKey(self.fps) & 0xFF
            if key in [27, 32, 13]:  # esc、Space、Enter
                break
            elif key == ord('s'):
                cv2.imwrite(f'{idx}.jpg', frame)
        cv2.destroyAllWindows()

    def show(self, start_frame: int = 0, frame_handler: Callable = None, window_name: str = 'Show Frame') -> timedelta:
        """显示画面,并且在关闭时返回最后显示画面的时间"""
        window_name = f'[{window_name}] press ESC/SPACE/ENTER to Exit, S to Save'
        tracker_name = 'Time'

        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_EXPANDED)
        with capture_video(self.path) as vc:
            total_seconds = int(self.frame_count / self.fps)
            if not start_frame:
                vc.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            cv2.createTrackbar(tracker_name, window_name, 0, total_seconds, lambda pos: vc.set(0, pos * 1000))
            cv2.setTrackbarPos(tracker_name, window_name, int(start_frame / self.fps))
            while True:
                ret, frame = vc.read()
                if not ret or frame is None:
                    raise AttributeError(f'read frame error. POS:{vc.get(0)}')
                if frame_handler:
                    frame = frame_handler(frame, self)
                cv2.imshow(window_name, frame)
                cv2.setTrackbarPos(tracker_name, window_name, int(vc.get(0) / 1000))
                key = cv2.waitKey(self.fps) & 0xFF
                if key in [27, 32, 13]:  # esc、Space、Enter
                    pos = cv2.getTrackbarPos(tracker_name, window_name)
                    break
                elif key == ord('s'):
                    cv2.imwrite(f'{idx}.jpg', frame)
        cv2.destroyAllWindows()
        return timedelta(seconds=pos)

    def select_roi(self, time_frame: str = '', frame_handler: Callable = None,
                   window_name: str = 'Select ROI') -> Tuple[int]:
        """以交互的方式剪切某一时刻的画面
        :return: tuple(矩形框中最小的x值, 矩形框中最小的y值, 矩形框的宽, 矩形框的高)
        """
        window_name = f'[{window_name}] press SPACE/ENTER to Confirm'
        frame_index = 0 if not time_frame else self.time_to_frame_idx(time_frame)
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_EXPANDED)
        frame = get_one_frame(self.path, frame_index)
        if frame_handler:
            frame = frame_handler(frame, self)
        roi = cv2.selectROI(window_name, frame, True, False)
        cv2.destroyAllWindows()
        return roi

    def select_threshold(self, time_frame: str = '', frame_handler: Callable = None,
                         default_pos=127, window_name: str = 'Select Threshold') -> int:
        window_name = f'[{window_name}] Press SPACE/ENTER to Confirm'
        tracker_name = 'threshold'
        threshold = default_pos

        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_EXPANDED)
        frame_index = 0 if not time_frame else self.time_to_frame_idx(time_frame)
        frame = get_one_frame(self.path, frame_index)
        if frame_handler:
            frame = frame_handler(frame, self)

        def set_threshold(pos):
            new_frame = FrameHandler.threshold(frame, self, pos)
            cv2.imshow(window_name, new_frame)

        cv2.namedWindow(window_name)
        cv2.createTrackbar(tracker_name, window_name, 0, 255, set_threshold)
        cv2.setTrackbarPos(trackbarname=tracker_name, winname=window_name, pos=default_pos)
        cv2.imshow(window_name, frame)
        if cv2.waitKey(0) in [32, 13]:
            threshold = cv2.getTrackbarPos(tracker_name, window_name)
        cv2.destroyAllWindows()
        return threshold

    @staticmethod
    def _check_file_type(file_type: str) -> str:
        file_type = file_type.lower()
        file_type_list = tuple(name
                               for name, obj in vars(SubtitleFormatter).items()
                               if isinstance(obj, property))
        if file_type not in file_type_list:
            raise AttributeError(f'supported file type:{file_type_list}, got {file_type}')
        return file_type

    def save_subtitle(self, subtitles: str, file_type: str) -> None:
        basename = os.path.basename(self.path)
        file_name, file_ext = os.path.splitext(basename)
        # file_path = f'output/{file_name}.{file_type}'
        os.makedirs(os.path.join(os.path.dirname(self.path), 'output'), exist_ok=True)
        file_path = os.path.join(os.path.dirname(self.path), 'output', f'{file_name}.{file_type}')

        with open(file_path, "w", encoding='utf-8') as file:
            file.write(subtitles)

    def save_subtitle_by_formatter(self, formatters: List[SubtitleFormatter], file_type: str = 'lrc') -> None:
        suffix = self._check_file_type(file_type)
        subtitle_list = [getattr(formatter, suffix) for formatter in formatters]
        content = '\n'.join(subtitle_list)
        self.save_subtitle(content, file_type)


class FrameHandler:
    @classmethod
    def resize(cls, frame, video: Video, resize: float = 0.5):
        if resize != 1:
            x, y = frame.shape[0:2]
            frame = cv2.resize(frame, (int(y * resize), int(x * resize)))
        return frame

    @classmethod
    def gray(cls, frame, video: Video):
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    @classmethod
    def roi(cls, frame, video: Video, r: Tuple):
        if r:
            frame = frame[int(r[1]):int(r[1] + r[3]), int(r[0]):int(r[0] + r[2])]
        return frame

    @classmethod
    def threshold(cls, frame, video: Video, threshold: int = 127):
        if threshold > 0:
            frame = cls.gray(frame, video)
            _, frame = cv2.threshold(frame, threshold, 255, cv2.THRESH_BINARY)
        return frame


class SubtitleExtractor:
    video: Video
    frame_handler: Callable  # func(frame, video: Video) -> frame
    time_start: str = ''
    time_end: str = ''
    threshold: int = -1
    roi_array: Tuple[int]

    def __init__(self, video_path: str, *, time_start: str = '', time_end: str = '', threshold=-1,
                 roi_array: Tuple[int] = (), frame_handler: Callable = None):
        self.video = Video(path=os.path.abspath(video_path))
        self.frame_handler = frame_handler
        self.roi_array = roi_array
        self.time_start = time_start
        self.time_end = time_end
        self.threshold = threshold
        self.table = str.maketrans('|', 'I', '<>{}[];`@#$%^*_=~\\')

    def ocr(self, ocr_handler, frame, frame_idx) -> List[Subtitle]:
        ocr_result = ocr_handler.ocr(frame, cls=True)
        if ocr_result[0] is None:
            return []

        return [
            Subtitle(
                box=res[0],
                # remove chars that are obviously ocr errors
                text=res[1][0].translate(self.table).strip(),
                score=res[1][1],
                frame_idx=frame_idx
            )
            for res in ocr_result[0]
        ]

    def _default_frame_handler(self, frame, video: Video):
        if self.frame_handler:
            frame = self.frame_handler(frame, video)
        if self.roi_array:
            frame = FrameHandler.roi(frame, video, self.roi_array)
        if self.threshold != -1:
            frame = FrameHandler.threshold(frame, video)
        return frame

    def _show(self):
        self.video.show_by_time_range(
            frame_handler=self._default_frame_handler,
            time_start=self.time_start,
            time_end=self.time_end,
            capture_interval=0,
            window_name='reshow'
        )

    def _to_formatter(self, subtitles: List[List[Subtitle]]) -> List[SubtitleFormatter]:
        if not subtitles:
            raise AttributeError('len(subtitles) == 0')

        last_sub_alive = subtitle_max_show_second * self.video.fps  # 最后一句字幕默认持续10s

        frame_idxes = [subs[0].frame_idx for subs in subtitles]
        frame_idxes.append(subtitles[-1][0].frame_idx + last_sub_alive)

        res = []
        for frame_idx in range(0, len(subtitles)):
            if not subtitles[frame_idx]:
                continue

            first_sub = subtitles[frame_idx][0]
            alive = frame_idxes[frame_idx + 1] - frame_idxes[frame_idx]  # 下一个字幕开始帧减去当前字幕开始帧
            start_second = first_sub.frame_idx / self.video.origin_fps
            end_second = min(start_second + subtitle_max_show_second,
                             (first_sub.frame_idx + alive) / self.video.origin_fps)
            res.append(SubtitleFormatter(
                content=' '.join([sub.text for sub in subtitles[frame_idx]]),  # 将同一帧的字幕都合并起来
                start_time=timedelta(seconds=start_second),
                end_time=timedelta(seconds=end_second)
            ))
        return res

    def select_fragment(self, reshow: bool = False) -> None:
        times: List[timedelta] = [self.video.show(0, self._default_frame_handler, '(1/2) Select StartTime')]
        start = int(times[0].total_seconds() * self.video.fps)  # 第二次从前面结束的地方开始
        times.append(self.video.show(start, self._default_frame_handler, '(2/2) Select EndTime'))
        self.time_start, self.time_end = [str(i) for i in sorted(times)]
        logging.info(f'[fragment] {self.time_start} -> {self.time_end}')
        if reshow:
            self._show()

    def select_roi(self, time_frame: str = '', reshow: bool = False) -> None:
        time_frame = time_frame if not time_frame else self.time_start
        self.roi_array = self.video.select_roi(time_frame=time_frame, frame_handler=self._default_frame_handler)
        logging.info(f'[roi array] {self.roi_array}')
        if reshow:
            self._show()

    def select_threshold(self, time_frame: str = '', reshow: bool = False) -> None:
        self.threshold = self.video.select_threshold(time_frame=time_frame, frame_handler=self._default_frame_handler)
        logging.info(f'[threshold] {self.threshold}')
        if reshow:
            self._show()

    def extract_by_func(self, *,
                        ocr_handler,
                        frame_handler: Callable,
                        time_start: str = '',
                        time_end: str = '',
                        capture_interval: float = 0.5,
                        ) -> List[List[Subtitle]]:
        subtitles = []
        # 非部署版本的paddleOCR不可同时识别多张图,是线程不安全的:https://aistudio.baidu.com/paddle/forum/topic/show/989282
        for idx, frame in tqdm(
                iterable=self.video.get_frames_by_time_range(time_start, time_end, capture_interval),
                total=self.video.count_frame(time_start, time_end, capture_interval),
                unit='帧'):
            if frame_handler:
                frame = frame_handler(frame, self.video)
            if frame is not None:
                subtitle = self.ocr(ocr_handler, frame, idx)
                subtitles.extend(subtitle)
        subs = SubtitleOption.clean(subtitles)
        return subs

    def extract(
            self, *,
            # ocr config
            lang: str = 'ch',
            use_angle_cls: bool = False,
            use_gpu: bool = False,
            use_mp: bool = True,
            enable_mkldnn: bool = False,
            gpu_mem: int = 1024,
            det_limit_side_len: int = 1920,
            rec_batch_num: int = 16,
            cpu_threads: int = 24,
            drop_score: float = 0.5,
            # video config
            time_start: str = '',
            time_end: str = '',
            capture_interval: float = 0.5,
            # handle frame config
            gray: bool = False,
            resize: float = 1,
    ) -> List[List[Subtitle]]:
        def frame_handler(frame, video: Video):
            if self.frame_handler:
                frame = self.frame_handler(frame, video)
            if self.roi_array:
                frame = FrameHandler.roi(frame, video, self.roi_array)
            if gray:
                frame = FrameHandler.gray(frame, video)
            if self.threshold != -1:
                frame = FrameHandler.threshold(frame, video, threshold=127)
            if resize != 1:
                frame = FrameHandler.resize(frame, video, resize)
            return frame

        from paddleocr import PaddleOCR, paddleocr  # 因为PaddleOCR需要加载大量数据到内存中，延迟导入
        paddleocr.logging.disable(logging.DEBUG)
        paddleocr.logging.disable(logging.WARNING)
        ocr_handler = PaddleOCR(lang=lang, use_angle_cls=use_angle_cls, use_gpu=use_gpu, drop_score=drop_score,
                                enable_mkldnn=False, use_mp=use_mp, det_limit_side_len=det_limit_side_len,
                                rec_batch_num=rec_batch_num, cpu_threads=cpu_threads, gpu_mem=gpu_mem)
        subtitles = self.extract_by_func(
            ocr_handler=ocr_handler,
            frame_handler=frame_handler,
            time_start=time_start if time_start else self.time_start,
            time_end=time_end if time_end else self.time_end,
            capture_interval=capture_interval
        )
        return subtitles

    def save(self, subtitles: List[List[Subtitle]], file_type: str = 'lrc') -> None:
        formatters = self._to_formatter(subtitles)
        self.video.save_subtitle_by_formatter(formatters, file_type)
        logging.info(f'{file_type} subtitle file has generated')


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', type=str, help='video path')
    parser.add_argument('--subtitle_max_show_second', type=int, default=10, help='subtitle max show second')
    parser.add_argument('--text_similar_threshold', type=int, default=70, help='text similar threshold')
    parser.add_argument('--output_format', type=str, default='lrc', help='subtitle file format')

    parser.add_argument('--ocr_lang', type=str, default='ch', help='ocr language')
    parser.add_argument('--ocr_use_angle_cls', type=bool, default=False, help='ocr use angle cls')
    parser.add_argument('--ocr_use_gpu', type=bool, default=False, help='ocr use gpu')
    parser.add_argument('--ocr_use_mp', type=bool, default=True, help='ocr use mp')
    parser.add_argument('--ocr_enable_mkldnn', type=bool, default=False, help='ocr enable mkldnn')
    parser.add_argument('--ocr_gpu_mem', type=int, default=1024, help='ocr gpu memory')
    parser.add_argument('--ocr_det_limit_side_len', type=int, default=1920, help='ocr det limit side len')
    parser.add_argument('--ocr_rec_batch_num', type=int, default=16, help='ocr rec batch num')
    parser.add_argument('--ocr_cpu_threads', type=int, default=64, help='ocr cpu threads')
    parser.add_argument('--ocr_drop_score', type=float, default=0.5, help='ocr drop score')

    parser.add_argument('--parse_time_start', type=str, default='', help='parse start time. format: %H:%M:%S"')
    parser.add_argument('--parse_time_end', type=str, default='', help='parse end time. format: %H:%M:%S')
    parser.add_argument('--parse_capture_interval', type=float, default=1, help='parse capture interval')
    parser.add_argument('--parse_gray', type=bool, default=False, help='parse gray frame')
    parser.add_argument('--parse_resize', type=float, default=1, help='parse resize frame')

    parser.add_argument('--use_fragment', type=bool, default=False, help='use fragment')
    parser.add_argument('--fragment_reshow', type=bool, default=False, help='reshow fragment selected frame')

    parser.add_argument('--use_roi', type=bool, default=False, help='use roi')
    parser.add_argument('--roi_time', type=str, help='select roi time. format: %H:%M:%S"')
    parser.add_argument('--roi_reshow', type=bool, default=False, help='reshow roi selected frame')

    parser.add_argument('--use_threshold', type=bool, default=False, help='use threshold')
    parser.add_argument('--threshold_time', type=str, help='select threshold time. format: %H:%M:%S"')
    parser.add_argument('--threshold_reshow', type=bool, default=True, help='reshow threshold selected frame')

    args = parser.parse_args()

    if not args.path:
        raise AttributeError("arg 'path' is null")
    if not args.threshold_time:
        args.threshold_time = args.roi_time
    if not args.roi_time:
        args.roi_time = threshold_time

    if args.fragment_reshow:
        args.use_fragment = True
    if args.roi_time or args.roi_reshow:
        args.use_roi = True

    if args.use_threshold:
        if args.threshold_time or args.threshold_reshow:
            args.use_threshold = True

    global subtitle_max_show_second
    global text_similar_threshold
    subtitle_max_show_second = args.subtitle_max_show_second
    text_similar_threshold = args.text_similar_threshold
    return args


def cmd_run() -> None:
    args = _parse_args()
    extractor = SubtitleExtractor(video_path=args.path)
    if args.use_fragment:
        extractor.select_fragment(reshow=args.fragment_reshow)
    if args.use_roi:
        extractor.select_roi(time_frame=args.roi_time, reshow=args.roi_reshow)
    if args.use_threshold:
        extractor.select_threshold(time_frame=args.threshold_time)
    subtitles = extractor.extract(
        lang=args.ocr_lang,
        use_angle_cls=args.ocr_use_angle_cls,
        use_gpu=args.ocr_use_gpu,
        use_mp=args.ocr_use_mp,
        enable_mkldnn=args.ocr_enable_mkldnn,
        gpu_mem=args.ocr_gpu_mem,
        det_limit_side_len=args.ocr_det_limit_side_len,
        rec_batch_num=args.ocr_rec_batch_num,
        cpu_threads=args.ocr_cpu_threads,
        drop_score=args.ocr_drop_score,
        time_start=args.parse_time_start,
        time_end=args.parse_time_end,
        capture_interval=args.parse_capture_interval,
        gray=args.parse_gray,
        resize=args.parse_resize,
    )
    extractor.save(subtitles, file_type=args.output_format)


def test():
    path = r'./CyberpunkEdgerunners01.mkv'
    extractor = SubtitleExtractor(video_path=path)
    extractor.select_fragment(reshow=True)
    extractor.select_roi(time_frame='3:24', reshow=True)
    extractor.select_threshold(time_frame='3:24', reshow=True)
    subtitles = extractor.extract(resize=0.5)
    extractor.save(subtitles, file_type='lrc')


def test2():
    path = 'test/series_1/麦乐鸡侠是啥梗.mp4'
    extractor = SubtitleExtractor(video_path=path, roi_array=(4, 345, 834, 120))
    subtitles = extractor.extract(lang='ch', resize=0.5)
    extractor.save(subtitles, file_type='txt')


def has_output_file(video_path, file_type):
    basename = os.path.basename(video_path)
    file_name, _ = os.path.splitext(basename)
    output_file_path = os.path.join(os.path.dirname(video_path), 'output', f'{file_name}.{file_type}')
    return os.path.exists(output_file_path)


video_info = {
    # Replace it with the data you need
    '1': (7, 350, 822, 72),
    '2': (9, 352, 831, 114),
    '3': (7, 397, 826, 70),
    '4': (9, 391, 830, 78),
    '5': (17, 344, 812, 114),
    '6': (9, 357, 829, 118),
    '7': (6, 406, 831, 68),
    '8': (4, 376, 838, 97),
    '9': (10, 367, 831, 101),
    '10': (6, 387, 838, 84)
}


def process_video(video_path):
    path = video_path[0]
    roi = video_info[video_path[1]]
    print(f"begin: {path}")
    extractor = SubtitleExtractor(video_path=path, roi_array=roi)
    subtitles = extractor.extract(lang='ch', resize=0.5)
    extractor.save(subtitles, file_type='txt')
    print(f"Processed: {path}")


def check_memory_usage():
    memory_percent = psutil.virtual_memory().percent
    if memory_percent > 90:
        print(f"Memory usage high ({memory_percent}%). Pausing for 60 seconds.")
        time.sleep(20)
        gc.collect()


def process_all_videos(file_type="txt"):
    assets_dir = '/root/autodl-tmp/progress/'
    video_files = []

    for root, _, files in os.walk(assets_dir):
        for file in files:
            if file.endswith('.mp4'):
                video_path = os.path.join(root, file)
                if not has_output_file(video_path, file_type):
                    video_files.append([video_path, root.split('/')[-2]])

    # 分批处理，每批 100 个视频
    batch_size = 100
    num_cores = multiprocessing.cpu_count()

    for i in range(0, len(video_files), batch_size):
        batch = video_files[i:i + batch_size]
        with ProcessPoolExecutor(max_workers=num_cores) as executor:
            executor.map(process_video, batch)

        check_memory_usage()
        gc.collect()


if __name__ == '__main__':
    process_all_videos(file_type="txt")
    # cmd_run()

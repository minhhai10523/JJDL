import os
import io
import time
import threading
import sqlite3
import requests
import logging
from concurrent.futures import ThreadPoolExecutor

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.behaviors import ButtonBehavior

from PIL import Image as PILImage
from PIL.ExifTags import TAGS

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Định nghĩa IconButton để tránh lỗi NameError
class IconButton(ButtonBehavior, Label):
    pass

# ===== GLOBALS & CONFIG =====
PARENT_FOLDER = "Picture AV"
THUMBNAIL_FOLDER = os.path.join(PARENT_FOLDER, "thumbnail")
DB_FILE = os.path.join(PARENT_FOLDER, "actors.db")

DETECTION_THRESHOLD = 5 * 1024
DOWNLOAD_THRESHOLD = 5 * 1024
MAX_THREADS = 4  # Giới hạn số luồng tải đồng thời

pause_event = threading.Event()
cancel_event = threading.Event()

all_images = {}  # all_images[page] = list of (file_path, texture) tuples

# ===== DB =====
def init_db():
    os.makedirs(PARENT_FOLDER, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS actors
                 (name TEXT PRIMARY KEY, folder_path TEXT, thumbnail_path TEXT)''')
    conn.commit()
    conn.close()

def update_actor_config(actor_name, folder_path, thumbnail_path):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO actors (name, folder_path, thumbnail_path) VALUES (?, ?, ?)",
              (actor_name, folder_path, thumbnail_path))
    conn.commit()
    conn.close()

def get_actor_history():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name, folder_path, thumbnail_path FROM actors")
    actors = [{"name": row[0], "folder_path": row[1], "thumbnail_path": row[2]} for row in c.fetchall()]
    conn.close()
    return actors

# ===== UTILS =====
def validate_actor_input(actor_input):
    if not actor_input or len(actor_input.strip()) < 3 or not actor_input.replace(" ", "").isalnum():
        return False, "Tên diễn viên phải dài ít nhất 3 ký tự và chỉ chứa chữ/số!"
    return True, ""

def process_actor_input(actor_input):
    slug = actor_input.lower().replace(" ", "-")
    tokens = slug.split('-')
    if len(tokens) == 4:
        sub_name = f"{tokens[0].title()} {tokens[1].title()} & {tokens[2].title()} {tokens[3].title()}"
    else:
        sub_name = slug.replace('-', ' ').title()
    folder_name = os.path.join(PARENT_FOLDER, sub_name)
    return slug, folder_name, sub_name

def fetch_image(url, timeout=10):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, stream=True, timeout=timeout)
        response.raise_for_status()
        if "404.Not.Found.svg" not in response.url:
            return response.content
        return None
    except requests.RequestException as e:
        logging.error(f"Error fetching {url}: {e}")
        return None

def download_image(url, save_path):
    try:
        image_data = fetch_image(url)
        if image_data and len(image_data) >= DOWNLOAD_THRESHOLD:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(image_data)
            return None
        return f"{os.path.basename(save_path)} (không đủ kích thước)"
    except Exception as e:
        logging.error(f"Error downloading {url}: {e}")
        return f"{os.path.basename(save_path)} ({str(e)})"

def correct_image_orientation(pil_image):
    try:
        exif = pil_image._getexif()
        if exif is not None:
            for tag, value in exif.items():
                if TAGS.get(tag) == 'Orientation':
                    if value == 3:
                        pil_image = pil_image.rotate(180, expand=True)
                    elif value == 6:
                        pil_image = pil_image.rotate(270, expand=True)
                    elif value == 8:
                        pil_image = pil_image.rotate(90, expand=True)
                    break
    except Exception as e:
        logging.warning(f"Error reading EXIF data: {e}")
    return pil_image

def pil_to_texture(pil_image):
    from kivy.core.image import Image as CoreImage
    data = io.BytesIO()
    pil_image.save(data, format="png")
    data.seek(0)
    core_image = CoreImage(data, ext="png")
    texture = core_image.texture
    return texture

# ===== KIVY KV STRING =====
KV = '''
ScreenManager:
    MainScreen:
    HistoryScreen:
    FullImageScreen:
    DownloadScreen:

<MainScreen>:
    name: "main"
    BoxLayout:
        orientation: "vertical"
        padding: dp(10)
        spacing: dp(10)
        Label:
            text: "Trình Tải Hình Ảnh AV (Kivy)"
            font_size: sp(18)
            size_hint_y: None
            height: self.texture_size[1] + dp(10)
            halign: 'center'
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(40)
            spacing: dp(5)
            TextInput:
                id: actor_input
                hint_text: "Nhập tên diễn viên..."
                text: "Kaori Yamashita"
            Button:
                text: "Tìm Kiếm"
                on_release: root.load_gallery()
        ScrollView:
            size_hint: (1,1)
            do_scroll_x: False
            do_scroll_y: True
            BoxLayout:
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                GridLayout:
                    id: gallery_grid
                    cols: 2
                    spacing: dp(5)
                    padding: dp(5)
                    size_hint_y: None
                    height: self.minimum_height
                Label:
                    id: empty_label
                    text: "Không có ảnh nào để hiển thị"
                    size_hint_y: None
                    height: dp(50) if self.text else 0
                    opacity: 1 if self.text else 0
        Label:
            id: status_label
            text: "Số trang phát hiện: 0"
            size_hint_y: None
            height: dp(30)
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(40)
            spacing: dp(5)
            Button:
                text: "Tải trang"
                on_release: root.download_page()
            Button:
                text: "Tải hết"
                on_release: root.download_all()
            Button:
                text: "Tải từ"
                on_release: root.show_range_popup()
            Button:
                text: "Lịch sử"
                on_release: root.open_history()
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(40)
            spacing: dp(5)
            Button:
                text: "Control"
                on_release: app.root.current = "download"
            Button:
                text: "Exit"
                on_release: root.confirm_exit()

<HistoryScreen>:
    name: "history"
    BoxLayout:
        orientation: "vertical"
        padding: dp(10)
        spacing: dp(10)
        Label:
            text: "Lịch Sử Diễn Viên"
            font_size: sp(18)
            size_hint_y: None
            height: self.texture_size[1] + dp(10)
            halign: 'center'
        TextInput:
            id: search_input
            hint_text: "Tìm kiếm diễn viên..."
            size_hint_y: None
            height: dp(40)
            on_text: root.filter_history(self.text)
        ScrollView:
            size_hint: (1,1)
            GridLayout:
                id: actor_history
                cols: 1
                spacing: dp(5)
                padding: dp(5)
                size_hint_y: None
                height: self.minimum_height
        Button:
            text: "<< Back"
            size_hint_y: None
            height: dp(40)
            on_release: app.root.current = "main"

<FullImageScreen>:
    name: "full_image"
    BoxLayout:
        orientation: "vertical"
        padding: dp(10)
        spacing: dp(10)
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(40)
            spacing: dp(5)
            Button:
                text: "<< Trang trước"
                on_release: root.prev_image()
            Button:
                text: "Toàn màn hình"
                on_release: root.toggle_fullscreen()
            Button:
                text: "Trang sau >>"
                on_release: root.next_image()
            Button:
                text: "Slideshow"
                on_release: root.toggle_slideshow()
            Button:
                text: "Tải trang"
                on_release: root.download_current_page()
            Button:
                text: "Quay lại"
                on_release: app.root.current = "main"
        Label:
            id: loading_label
            text: ""
            size_hint_y: None
            height: dp(30)
            halign: 'center'
        ScrollView:
            size_hint: (1,1)
            do_scroll_x: False
            do_scroll_y: True
            GridLayout:
                id: page_images_grid
                cols: 2
                spacing: dp(5)
                padding: dp(5)
                size_hint_y: None
                height: self.minimum_height

<DownloadScreen>:
    name: "download"
    BoxLayout:
        orientation: "vertical"
        padding: dp(10)
        spacing: dp(10)
        Label:
            text: "Chức năng Download"
            font_size: sp(18)
            size_hint_y: None
            height: self.texture_size[1] + dp(10)
            halign: 'center'
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(40)
            spacing: dp(5)
            Button:
                text: "⏸ Pause"
                on_release: root.pause_download()
            Button:
                text: "▶ Resume"
                on_release: root.resume_download()
            Button:
                text: "✖ Cancel"
                on_release: root.cancel_download()
            Button:
                text: "<< Back"
                on_release: app.root.current = "main"
        ProgressBar:
            id: progress_bar
            max: 100
            value: 0
        Label:
            id: progress_label
            text: "0%"
            size_hint_y: None
            height: dp(30)

<IconButton@ButtonBehavior+Label>:
    text: ''
    size_hint: None, None
    size: dp(40), dp(40)
    font_size: sp(24)
    halign: 'center'
    valign: 'middle'
'''

# ===== MÀN HÌNH CHÍNH =====
class MainScreen(Screen):
    total_pages_detected = 0

    def on_pre_enter(self):
        init_db()

    def load_gallery(self):
        actor_input = self.ids.actor_input.text.strip()
        is_valid, error_msg = validate_actor_input(actor_input)
        if not is_valid:
            self.show_popup("Lỗi", error_msg)
            return
        slug, folder_name, sub_name = process_actor_input(actor_input)
        self.ids.status_label.text = "Đang tải..."
        self.ids.gallery_grid.clear_widgets()
        self.ids.empty_label.text = ""  # Reset thông báo trống
        self.total_pages_detected = 0

        base_url = f"https://jjgirls.com/japanese/{slug}"

        def fetch_page(page):
            local_preview_path = os.path.join(folder_name, f"{os.path.basename(folder_name)}-{page}-1.jpg")
            if os.path.exists(local_preview_path):
                return local_preview_path
            preview_url = f"{base_url}/{page}/{slug}-1.jpg"
            image_data = fetch_image(preview_url)
            if image_data and len(image_data) >= DETECTION_THRESHOLD:
                os.makedirs(folder_name, exist_ok=True)
                try:
                    with open(local_preview_path, "wb") as f:
                        f.write(image_data)
                    return local_preview_path
                except Exception as e:
                    logging.error(f"Error saving image {local_preview_path}: {e}")
                    return None
            return None

        def add_thumbnail(page, image_path):
            try:
                pil_img = PILImage.open(image_path)
                if pil_img.format not in ["JPEG", "PNG"]:
                    raise ValueError("Định dạng ảnh không được hỗ trợ")
                pil_img = correct_image_orientation(pil_img)
                pil_img.thumbnail((300, 300), PILImage.Resampling.LANCZOS)
                texture = pil_to_texture(pil_img)

                container = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(350))
                img_widget = KivyImage(texture=texture, size_hint=(1, None), height=dp(300), fit_mode='contain')
                img_widget.bind(on_touch_down=lambda instance, touch: self.on_image_touch(instance, touch, page, folder_name, slug, sub_name))
                label = Label(text=f"Trang {page}", size_hint_y=None, height=dp(50), halign='center')
                
                container.add_widget(img_widget)
                container.add_widget(label)
                self.ids.gallery_grid.add_widget(container)
            except Exception as e:
                logging.error(f"Error processing thumbnail for page {page}: {e}")
                Clock.schedule_once(lambda dt: self.show_popup("Lỗi", f"Không thể hiển thị ảnh trang {page}: {str(e)}"))

        def load_pages():
            page = 1
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                while True:
                    image_path = fetch_page(page)
                    if image_path:
                        self.total_pages_detected = page
                        Clock.schedule_once(lambda dt, p=page, path=image_path: add_thumbnail(p, path))
                        page += 1
                    else:
                        break
            Clock.schedule_once(lambda dt: self.update_status())
            if self.total_pages_detected > 0:
                preview_path = os.path.join(folder_name, f"{os.path.basename(folder_name)}-1-1.jpg")
                if os.path.exists(preview_path):
                    update_actor_config(sub_name, folder_name,
                        os.path.join(THUMBNAIL_FOLDER, f"{sub_name.lower().replace(' ', '-')}-thumb.jpg"))

        threading.Thread(target=load_pages, daemon=True).start()

    def on_image_touch(self, instance, touch, page, folder_name, slug, sub_name):
        if instance.collide_point(*touch.pos) and touch.button == 'left':
            self.open_full_image(page, folder_name, slug, sub_name)

    def update_status(self):
        self.ids.status_label.text = f"Số trang phát hiện: {self.total_pages_detected}"
        if self.total_pages_detected > 0 and not self.ids.gallery_grid.children:
            self.ids.empty_label.text = "Không có ảnh nào để hiển thị"

    def open_full_image(self, page, folder_name, slug, sub_name):
        if page not in all_images:
            self.load_page_images(page, folder_name, slug)
        full_screen = self.manager.get_screen("full_image")
        full_screen.current_page = page
        full_screen.folder_name = folder_name
        full_screen.slug = slug
        full_screen.sub_name = sub_name
        full_screen.load_current_page()
        self.manager.current = "full_image"

    def load_page_images(self, page, folder_name, slug):
        if page in all_images:
            return
        base_url = f"https://jjgirls.com/japanese/{slug}"
        all_images[page] = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for img_num in range(1, 13):
                local_img_path = os.path.join(folder_name, f"{os.path.basename(folder_name)}-{page}-{img_num}.jpg")
                if os.path.exists(local_img_path):
                    pil_img = PILImage.open(local_img_path)
                    pil_img = correct_image_orientation(pil_img)
                    pil_img.thumbnail((400, 400), PILImage.Resampling.LANCZOS)
                    texture = pil_to_texture(pil_img)
                    all_images[page].append((local_img_path, texture))
                else:
                    img_url = f"{base_url}/{page}/{slug}-{img_num}.jpg"
                    futures.append(executor.submit(download_image, img_url, local_img_path))
            for future in futures:
                result = future.result()
                if not result:  # No error
                    local_img_path = os.path.join(folder_name, f"{os.path.basename(folder_name)}-{page}-{len(all_images[page]) + 1}.jpg")
                    pil_img = PILImage.open(local_img_path)
                    pil_img = correct_image_orientation(pil_img)
                    pil_img.thumbnail((400, 400), PILImage.Resampling.LANCZOS)
                    texture = pil_to_texture(pil_img)
                    all_images[page].append((local_img_path, texture))

    def download_page(self):
        actor_input = self.ids.actor_input.text.strip()
        is_valid, error_msg = validate_actor_input(actor_input)
        if not is_valid:
            self.show_popup("Lỗi", error_msg)
            return
        slug, folder_name, sub_name = process_actor_input(actor_input)
        threading.Thread(target=self.download_page_images, args=(1, slug, folder_name, sub_name), daemon=True).start()

    def download_all(self):
        actor_input = self.ids.actor_input.text.strip()
        is_valid, error_msg = validate_actor_input(actor_input)
        if not is_valid:
            self.show_popup("Lỗi", error_msg)
            return
        slug, folder_name, sub_name = process_actor_input(actor_input)
        threading.Thread(target=self.download_all_images, args=(slug, folder_name, sub_name), daemon=True).start()

    def show_range_popup(self):
        content = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(10))
        start_input = TextInput(hint_text="Trang bắt đầu", input_filter='int')
        end_input = TextInput(hint_text="Trang kết thúc", input_filter='int')
        submit_btn = Button(text="Tải", size_hint_y=None, height=dp(40))
        content.add_widget(start_input)
        content.add_widget(end_input)
        content.add_widget(submit_btn)
        popup = Popup(title="Chọn phạm vi tải", content=content, size_hint=(0.8, 0.5))
        submit_btn.bind(on_release=lambda _: self.download_range(start_input.text, end_input.text, popup))
        popup.open()

    def download_range(self, start_text, end_text, popup):
        popup.dismiss()
        try:
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                self.show_popup("Lỗi", "Phạm vi không hợp lệ!")
                return
        except ValueError:
            self.show_popup("Lỗi", "Vui lòng nhập số hợp lệ!")
            return
        actor_input = self.ids.actor_input.text.strip()
        is_valid, error_msg = validate_actor_input(actor_input)
        if not is_valid:
            self.show_popup("Lỗi", error_msg)
            return
        slug, folder_name, sub_name = process_actor_input(actor_input)
        threading.Thread(target=self.download_range_images, args=(start, end, slug, folder_name, sub_name), daemon=True).start()

    def download_page_images(self, page, slug, folder_name, sub_name):
        base_url = f"https://jjgirls.com/japanese/{slug}"
        errors = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for img_num in range(1, 13):
                if cancel_event.is_set():
                    Clock.schedule_once(lambda dt: self.show_popup("Thông báo", f"Tải trang {page} bị hủy!"))
                    return
                while pause_event.is_set() and not cancel_event.is_set():
                    time.sleep(0.1)
                img_url = f"{base_url}/{page}/{slug}-{img_num}.jpg"
                filename = f"{os.path.basename(folder_name)}-{page}-{img_num}.jpg"
                save_path = os.path.join(folder_name, filename)
                if os.path.exists(save_path):
                    continue
                futures.append(executor.submit(download_image, img_url, save_path))
            for future in futures:
                result = future.result()
                if result:
                    errors.append(result)
        if errors:
            Clock.schedule_once(lambda dt: self.show_popup("Cảnh báo", f"Có lỗi tải: {', '.join(errors)}"))
        else:
            Clock.schedule_once(lambda dt: self.show_popup("Thông báo", f"Trang {page} đã được tải"))

    def download_all_images(self, slug, folder_name, sub_name):
        total_pages = self.total_pages_detected
        if total_pages < 1:
            Clock.schedule_once(lambda dt: self.show_popup("Lỗi", "Chưa phát hiện trang nào!"))
            return
        base_url = f"https://jjgirls.com/japanese/{slug}"
        total_images = total_pages * 12
        counter = 0
        errors = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for page in range(1, total_pages + 1):
                for img_num in range(1, 13):
                    if cancel_event.is_set():
                        Clock.schedule_once(lambda dt: self.show_popup("Thông báo", "Download tất cả bị hủy!"))
                        return
                    while pause_event.is_set() and not cancel_event.is_set():
                        time.sleep(0.1)
                    img_url = f"{base_url}/{page}/{slug}-{img_num}.jpg"
                    filename = f"{os.path.basename(folder_name)}-{page}-{img_num}.jpg"
                    save_path = os.path.join(folder_name, filename)
                    if os.path.exists(save_path):
                        counter += 1
                        continue
                    futures.append(executor.submit(download_image, img_url, save_path))
            for future in futures:
                result = future.result()
                if result:
                    errors.append(result)
                else:
                    counter += 1
                percent = (counter / total_images) * 100
                Clock.schedule_once(lambda dt, p=percent: self.update_progress(p))
        if errors:
            Clock.schedule_once(lambda dt: self.show_popup("Cảnh báo", f"Lỗi: {', '.join(errors)}"))
        else:
            Clock.schedule_once(lambda dt: self.show_popup("Thông báo", "Download tất cả hoàn tất!"))

    def download_range_images(self, start, end, slug, folder_name, sub_name):
        base_url = f"https://jjgirls.com/japanese/{slug}"
        total_images = (end - start + 1) * 12
        counter = 0
        errors = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for page in range(start, end + 1):
                for img_num in range(1, 13):
                    if cancel_event.is_set():
                        Clock.schedule_once(lambda dt: self.show_popup("Thông báo", "Download theo phạm vi bị hủy!"))
                        return
                    while pause_event.is_set() and not cancel_event.is_set():
                        time.sleep(0.1)
                    img_url = f"{base_url}/{page}/{slug}-{img_num}.jpg"
                    filename = f"{os.path.basename(folder_name)}-{page}-{img_num}.jpg"
                    save_path = os.path.join(folder_name, filename)
                    if os.path.exists(save_path):
                        counter += 1
                        continue
                    futures.append(executor.submit(download_image, img_url, save_path))
            for future in futures:
                result = future.result()
                if result:
                    errors.append(result)
                else:
                    counter += 1
                percent = (counter / total_images) * 100
                Clock.schedule_once(lambda dt, p=percent: self.update_progress(p))
        if errors:
            Clock.schedule_once(lambda dt: self.show_popup("Cảnh báo", f"Lỗi: {', '.join(errors)}"))
        else:
            Clock.schedule_once(lambda dt: self.show_popup("Thông báo", f"Download phạm vi {start}-{end} hoàn tất!"))

    def update_progress(self, percent):
        screen = self.manager.get_screen("download")
        screen.ids.progress_bar.value = percent
        screen.ids.progress_label.text = f"{percent:.1f}%"

    def open_history(self):
        hist_screen = self.manager.get_screen("history")
        hist_screen.refresh_actor_history()
        self.manager.current = "history"

    def confirm_exit(self):
        content = BoxLayout(orientation='vertical', padding=dp(10))
        content.add_widget(Label(text="Bạn có chắc muốn thoát?"))
        btn_box = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(5))
        yes_btn = Button(text="Có")
        no_btn = Button(text="Không")
        btn_box.add_widget(yes_btn)
        btn_box.add_widget(no_btn)
        content.add_widget(btn_box)
        popup = Popup(title="Xác nhận", content=content, size_hint=(0.6, 0.4))
        yes_btn.bind(on_release=lambda _: App.get_running_app().stop())
        no_btn.bind(on_release=lambda _: popup.dismiss())
        popup.open()

    def show_popup(self, title, message):
        popup = Popup(title=title, content=Label(text=message), size_hint=(None, None), size=(300, 200))
        popup.open()

# ===== MÀN HÌNH LỊCH SỬ =====
class HistoryScreen(Screen):
    def refresh_actor_history(self, filter_text=""):
        layout = self.ids.actor_history
        layout.clear_widgets()
        actors = get_actor_history()
        actors.sort(key=lambda x: x["name"].lower())
        for act in actors:
            if filter_text.lower() in act["name"].lower():
                btn = Button(text=act["name"], size_hint_y=None, height=dp(40))
                btn.bind(on_release=lambda b: self.select_actor(b.text))
                layout.add_widget(btn)

    def filter_history(self, text):
        self.refresh_actor_history(text)

    def select_actor(self, actor_name):
        main_scr = self.manager.get_screen("main")
        main_scr.ids.actor_input.text = actor_name
        self.manager.current = "main"

# ===== MÀN HÌNH XEM ẢNH FULL =====
class FullImageScreen(Screen):
    current_page = 1
    folder_name = ""
    slug = ""
    sub_name = ""
    auto_run = False

    def load_current_page(self):
        self.ids.loading_label.text = "Đang tải..."
        if self.current_page not in all_images:
            self.load_page_images(self.current_page)
        images_list = all_images.get(self.current_page, [])
        self.ids.page_images_grid.clear_widgets()
        if images_list:
            for img_path, texture in images_list:
                try:
                    img_widget = KivyImage(texture=texture, size_hint_y=None, height=dp(400), fit_mode='contain')
                    img_widget.bind(on_touch_down=lambda instance, touch, path=img_path, tex=texture: self.on_image_touch(instance, touch, path, tex))
                    self.ids.page_images_grid.add_widget(img_widget)
                except Exception as e:
                    logging.error(f"Error displaying image {img_path}: {e}")
                    self.show_popup("Lỗi", f"Không thể hiển thị ảnh: {str(e)}")
        self.ids.loading_label.text = ""

    def on_image_touch(self, instance, touch, img_path, texture):
        if instance.collide_point(*touch.pos) and touch.button == 'left':
            images_list = all_images.get(self.current_page, [])
            current_index = next(i for i, (path, _) in enumerate(images_list) if path == img_path)
            self.show_enlarged_image(images_list, current_index)

    def show_enlarged_image(self, images_list, current_index):
        popup = Popup(title="", size_hint=(1, 1), auto_dismiss=False)
        layout = FloatLayout()

        img_widget = KivyImage(fit_mode='contain', size_hint=(1, 1))
        layout.add_widget(img_widget)

        # Nút "X" ở góc trái trên
        close_btn = IconButton(text="✖", pos_hint={'top': 1, 'left': 0}, x=dp(10), y=Window.height - dp(50))
        layout.add_widget(close_btn)

        # Nút "Play" ở góc phải trên
        play_btn = IconButton(text="▶", pos_hint={'top': 1, 'right': 1}, x=Window.width - dp(50), y=Window.height - dp(50))
        layout.add_widget(play_btn)

        # Nhãn hiển thị số ảnh
        nav_label = Label(text="", size_hint=(None, None), size=(dp(100), dp(30)), pos_hint={'center_x': 0.5, 'top': 1}, y=Window.height - dp(50))
        layout.add_widget(nav_label)

        popup.content = layout

        slideshow_running = [False]  # Sử dụng list để có thể thay đổi giá trị trong closure
        slideshow_event = [None]
        last_swipe_time = [0]  # Thời gian lần trượt cuối cùng để chống lặp

        def update_image(index):
            img_path, texture = images_list[index]
            # Tính toán kích thước để hiển thị đầy màn hình
            texture_width, texture_height = texture.size
            aspect_ratio = texture_width / texture_height
            window_ratio = Window.width / Window.height

            if aspect_ratio > window_ratio:  # Ảnh rộng hơn cửa sổ
                new_width = Window.width
                new_height = int(Window.width / aspect_ratio)
            else:  # Ảnh cao hơn cửa sổ
                new_height = Window.height
                new_width = int(Window.height * aspect_ratio)

            img_widget.texture = texture
            img_widget.size = (new_width, new_height)
            img_widget.size_hint = (None, None)
            img_widget.pos_hint = {'center_x': 0.5, 'center_y': 0.5}
            nav_label.text = f"Ảnh {index + 1}/{len(images_list)}"

        touch_start_x = [None]  # Vị trí bắt đầu chạm

        def on_touch_down(instance, touch):
            touch_start_x[0] = touch.x
            return True

        def on_touch_up(instance, touch):
            nonlocal current_index  # Khai báo nonlocal ngay đầu hàm
            if touch.is_double_tap or touch.is_mouse_scrolling:
                return False

            current_time = time.time()
            if current_time - last_swipe_time[0] < 0.3:  # Chống lặp: chỉ xử lý trượt sau 0.3 giây
                return False

            if touch_start_x[0] is not None:
                dx = touch.x - touch_start_x[0]
                if dx < -50:  # Trượt sang trái -> ảnh sau
                    if current_index < len(images_list) - 1:
                        current_index += 1
                        update_image(current_index)
                        last_swipe_time[0] = current_time
                elif dx > 50:  # Trượt sang phải -> ảnh trước
                    if current_index > 0:
                        current_index -= 1
                        update_image(current_index)
                        last_swipe_time[0] = current_time
            touch_start_x[0] = None
            return True

        def start_slideshow(_):
            if slideshow_running[0]:
                slideshow_running[0] = False
                if slideshow_event[0]:
                    Clock.unschedule(slideshow_event[0])
                play_btn.text = "▶"
            else:
                slideshow_running[0] = True
                play_btn.text = "⏸"

                def slideshow_step(dt):
                    nonlocal current_index
                    if current_index < len(images_list) - 1:
                        current_index += 1
                    else:
                        current_index = 0
                    update_image(current_index)

                slideshow_event[0] = Clock.schedule_interval(slideshow_step, 2)

        img_widget.bind(on_touch_down=on_touch_down)
        img_widget.bind(on_touch_up=on_touch_up)
        close_btn.bind(on_release=popup.dismiss)
        play_btn.bind(on_release=start_slideshow)

        update_image(current_index)
        popup.open()

    def prev_image(self):
        if self.current_page > 1:
            self.current_page -= 1
            if self.current_page not in all_images:
                self.load_page_images(self.current_page)
            self.load_current_page()

    def next_image(self):
        main_screen = self.manager.get_screen("main")
        if self.current_page < main_screen.total_pages_detected:
            self.current_page += 1
            if self.current_page not in all_images:
                self.load_page_images(self.current_page)
            self.load_current_page()
        elif self.auto_run:
            self.toggle_slideshow()
            self.show_popup("Thông báo", "Đã đến trang cuối cùng!")

    def load_page_images(self, page):
        if page in all_images:
            return
        base_url = f"https://jjgirls.com/japanese/{self.slug}"
        all_images[page] = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for img_num in range(1, 13):
                local_img_path = os.path.join(self.folder_name, f"{os.path.basename(self.folder_name)}-{page}-{img_num}.jpg")
                if os.path.exists(local_img_path):
                    pil_img = PILImage.open(local_img_path)
                    pil_img = correct_image_orientation(pil_img)
                    pil_img.thumbnail((400, 400), PILImage.Resampling.LANCZOS)
                    texture = pil_to_texture(pil_img)
                    all_images[page].append((local_img_path, texture))
                else:
                    img_url = f"{base_url}/{page}/{self.slug}-{img_num}.jpg"
                    futures.append(executor.submit(download_image, img_url, local_img_path))
            for future in futures:
                result = future.result()
                if not result:  # No error
                    local_img_path = os.path.join(self.folder_name, f"{os.path.basename(self.folder_name)}-{page}-{len(all_images[page]) + 1}.jpg")
                    pil_img = PILImage.open(local_img_path)
                    pil_img = correct_image_orientation(pil_img)
                    pil_img.thumbnail((400, 400), PILImage.Resampling.LANCZOS)
                    texture = pil_to_texture(pil_img)
                    all_images[page].append((local_img_path, texture))

    def toggle_fullscreen(self):
        app = App.get_running_app()
        app.root_window.fullscreen = not app.root_window.fullscreen

    def toggle_slideshow(self):
        self.auto_run = not self.auto_run
        if self.auto_run:
            self.start_slideshow()
        else:
            if hasattr(self, "slideshow_event") and self.slideshow_event:
                Clock.unschedule(self.slideshow_event)

    def start_slideshow(self):
        self.slideshow_event = Clock.schedule_interval(lambda dt: self.next_image(), 2)

    def download_current_page(self):
        main_screen = self.manager.get_screen("main")
        threading.Thread(target=main_screen.download_page_images, args=(self.current_page, self.slug, self.folder_name, self.sub_name), daemon=True).start()

    def show_popup(self, title, message):
        popup = Popup(title=title, content=Label(text=message), size_hint=(None, None), size=(300, 200))
        popup.open()

# ===== MÀN HÌNH ĐIỀU KHIỂN DOWNLOAD =====
class DownloadScreen(Screen):
    def pause_download(self):
        pause_event.set()
        self.ids.progress_label.text = "Paused"

    def resume_download(self):
        pause_event.clear()
        self.ids.progress_label.text = "Resumed"

    def cancel_download(self):
        cancel_event.set()
        self.ids.progress_label.text = "Cancelled"

# ===== MAIN APP =====
class AVDownloaderApp(App):
    def build(self):
        Window.size = (800, 600)  # Đặt kích thước cửa sổ mặc định
        self.title = "Trình Tải Hình Ảnh AV (Kivy)"
        return Builder.load_string(KV)

if __name__ == "__main__":
    AVDownloaderApp().run()

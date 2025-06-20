import numpy as np
from numpy.typing import NDArray
import os

from pyqtgraph import (
    PlotWidget, ViewBox, PlotItem, setConfigOptions,
    TextItem, PlotDataItem, ScatterPlotItem, InfiniteLine,
    mkPen, mkBrush
)

from PyQt6.QtGui import (
    QKeyEvent, QWheelEvent, QMouseEvent, QColor, 
    QGuiApplication, QCursor, QAction
)
from PyQt6.QtCore import Qt, QPointF, QTimer, QObject, QEvent

from PyQt6.QtWidgets import (
    QPushButton, QVBoxLayout, QLabel, QDialog, 
    QTextEdit, QMessageBox, QMenu
)

from EPGData import EPGData
from Settings import Settings
from LabelArea import LabelArea
from CommentMarker import CommentMarker
from SelectionManager import Selection


# DEBUG ONLY TODO remove imports for testing
from PyQt6.QtWidgets import QApplication  
import sys, time


if os.name == "nt":
    print("Windows detected, running with OpenGL")
    setConfigOptions(useOpenGL = True)

class PanZoomViewBox(ViewBox):
    """
    Custom ViewBox that overrides default mouse/scroll behavior to support
    pan and zoom using wheel + modifiers.

    Pan/Zoom behavior:
    - Ctrl + Scroll: horizontal/vertical zoom (with Shift)
    - Scroll only: pan (horizontal or vertical based on Shift)
    """

    def __init__(self) -> None:
        super().__init__()
        self.datawindow: DataWindow = None

    def wheelEvent(self, event: QWheelEvent, axis=None) -> None:
        """
        Handles wheel input for zooming and panning, based on modifier keys.

        - Ctrl: zoom
        - Shift: vertical zoom or pan
        - No modifiers: horizontal pan
        """
        if self.datawindow is None:
            self.datawindow = self.parentItem().getViewWidget()
        
        delta = event.angleDelta().y()
        modifiers = event.modifiers()

        ctrl_held = modifiers & Qt.KeyboardModifier.ControlModifier
        shift_held = modifiers & Qt.KeyboardModifier.ShiftModifier

        if ctrl_held:
            zoom_factor = 1.001**delta
            center = self.mapToView(event.position())
            if shift_held: 
                self.scaleBy((1, 1 / zoom_factor), center)
            else:
                self.scaleBy((1 / zoom_factor, 1), center)
        else:
            (x_min, x_max), (y_min, y_max) = self.viewRange()
            width, height = x_max - x_min, y_max - y_min

            if shift_held:
                v_zoom_factor = 5e-4
                dy = delta * v_zoom_factor * height
                self.translateBy(y=dy)
            else:
                h_zoom_factor = 2e-4
                dx = delta * h_zoom_factor * width

                new_x_min = x_min + dx

                # don't pan if it moves x=0 more than halfway across the ViewBox
                if 0 > new_x_min + 0.5 * width:
                    pass  
                else:
                    self.translateBy(x=dx)

        event.accept()
        self.datawindow.update_plot()

    def mouseDragEvent(self, event, axis=None) -> None:
        """
        Disables default drag-to-pan behavior (drag is used for selections).
        """
        event.ignore()


    def contextMenuEvent(self, event):
        """
        Displays a context menu for right-clicking on LabelAreas.

        Currently adds a label type submenu to change the label's classification.
        """
        if self.datawindow is None:
            self.datawindow = self.parentItem().getViewWidget()

        item = self.datawindow.selection.hovered_item
        if isinstance(item, InfiniteLine):
            print('Right-clicked InfiniteLine')
            return  # TODO: infinite line context menu not yet implemented
        
        menu = QMenu()
        label_type_dropdown = QMenu("Change Label Type", menu)

        label_names = list(Settings.label_to_color.keys())
        label_names.remove("END AREA")
        for label in label_names:            
            action = QAction(label, menu)
            action.setCheckable(True)

            if item.label == label:
                action.setChecked(True)
                
            action.triggered.connect(
                lambda checked, label_area=item, label=label:
                self.datawindow.selection.change_label_type(label_area, label)
            )
        
            label_type_dropdown.addAction(action)

        action2 = QAction("Custom Option 2")
        menu.addMenu(label_type_dropdown)
        menu.addAction(action2)

        action = menu.exec(event.screenPos())
        if action == label_type_dropdown:
            print("Option 1 selected")
        elif action == action2:
            print("Option 2 selected")



class DataWindow(PlotWidget):
    """
    Main widget for visualizing waveform recordings.

    Includes:
    - Zooming and panning (via `PanZoomViewBox`)
    - Interactive label areas
    - Transition lines
    - Baseline editing
    - Comment markers
    - Compression and zoom indicators

    Also handles data loading, rendering, and downsampling for performance.
    """
    def __init__(self, epgdata: EPGData) -> None:
        """
        Initializes the DataWindow with plotting elements, UI overlays, and input handling.

        Parameters:
            epgdata (EPGData): The waveform and label data source.
        """
        # UI ITEMS
        super().__init__(viewBox=PanZoomViewBox())
        self.plot_item: PlotItem = self.getPlotItem() # the plotting canvas (axes, grid, data, etc.)
        self.plot_item.hideButtons()
        self.viewbox: PanZoomViewBox = self.plot_item.getViewBox() # the plotting area (no axes, etc.)
        self.viewbox.datawindow = self
        self.viewbox.menu = None  # disable default menu
        self.viewbox.sigRangeChanged.connect(self.update_plot)  # update plot on viewbox change

        # DATA
        self.epgdata: EPGData = epgdata
        self.file: str = None
        self.prepost: str = "pre"
        
        self.xy_data: list[NDArray] = []  # x and y data actually rendered to the screen
        self.curve: PlotDataItem = PlotDataItem(antialias=False) 
        self.scatter: ScatterPlotItem = ScatterPlotItem(
            symbol="o", size=4, brush="blue"
        )  # the discrete points shown at high zooms
        self.initial_downsampled_data: list[NDArray, NDArray]  # cache of the dataset after the initial downsample

        # CURSOR
        self.last_cursor_pos: QPointF = None # last cursor pos rel. to top left of application
        # self.cursor_mode: str = "normal"  # cursor state, e.g. normal, baseline selection

        # INDICATORS & LABELS
        self.compression: float = 0
        self.compression_text: TextItem = TextItem()
        self.zoom_level: float = 1
        self.zoom_text: TextItem = TextItem()
        #self.transitions: list[tuple[float, str]] = []   # the x-values of each label transition
        self.transition_mode: str = 'labels'
        self.labels: list[LabelArea] = []  # the list of LabelAreas

        # SELECTION
        self.selection: Selection = Selection(self)
        self.moving_mode: bool = False  # whether an interactive item is being moved
        self.edit_mode_enabled: bool = True  # whether the labels can be interacted with

        # BASELINE
        self.baseline: InfiniteLine = InfiniteLine(
            angle = 0, movable=False, pen=mkPen("gray", width = 3)
        )
        self.plot_item.addItem(self.baseline)
        self.baseline.setVisible(False)
        
        self.baseline_preview: InfiniteLine = InfiniteLine(
            angle = 0, movable = False,
            pen=mkPen("gray", style = Qt.PenStyle.DashLine, width = 3),
        )

        self.addItem(self.baseline_preview)
        self.baseline_preview.setVisible(False)
        self.baseline_preview_enabled: bool = False

        # COMMENTS
        self.comments: dict[float, CommentMarker] = {} # the dict of Comments
        self.comment_editing = False

        self.comment_preview: InfiniteLine = InfiniteLine(
            angle = 90, movable = False,
            pen=mkPen("gray", style = Qt.PenStyle.DashLine, width = 3),
        )

        self.addItem(self.comment_preview)
        self.comment_preview.setVisible(False)

        self.comment_preview_enabled: bool = False
        self.moving_comment: CommentMarker = None

        self.initUI()

    def initUI(self) -> None:
        """
        Initializes plot appearance, UI layout, axes labels, and placeholder data.
        Called once during setup.
        """
        self.chart_width: int = 400
        self.chart_height: int = 400
        self.setGeometry(0, 0, self.chart_width, self.chart_height)

        self.setBackground("white")
        self.setTitle("<b>SCIDO Waveform Editor</b>", color="black", size="12pt")

        self.viewbox.setBorder(mkPen("black", width=3))

        self.plot_item.addItem(self.curve)
        self.plot_item.addItem(self.scatter)
        self.plot_item.setLabel("bottom", "<b>Time [s]</b>", color="black")
        self.plot_item.setLabel("left", "<b>Voltage [V]</b>", color="black")
        self.plot_item.showGrid(x=Settings.show_grid, y=Settings.show_grid)
        self.plot_item.layout.setContentsMargins(30, 30, 30, 20)
        self.plot_item.enableAutoRange(False)

        # placeholder sine wave
        self.xy_data.append(np.linspace(0, 1, 10000))
        self.xy_data.append(np.sin(2 * np.pi * self.xy_data[0]))
        self.curve.setData(
            self.xy_data[0], self.xy_data[1], pen=mkPen(color="b", width=2)
        )

        self.curve.setClipToView(False)  # already done in manual downsampling
        self.scatter.setVisible(False)
        self.curve.setZValue(-5)
        self.scatter.setZValue(-4)

        QTimer.singleShot(0, self.deferred_init)

        ## DEBUG/DEV TOOLS
        self.enable_debug = False
        self.debug_boxes = []



    def deferred_init(self) -> None:
        """
        Defers adding compression/zoom overlays until the scene is ready.
        """
        self.compression = 0
        self.compression_text = TextItem(
            text=f"Compression: {self.compression: .1f}", color="black", anchor=(0, 0)
        )
        self.compression_text.setPos(QPointF(80, 15))
        self.scene().addItem(self.compression_text)

        self.zoom_level = 1
        self.zoom_text = TextItem(
            text=f"Zoom: {self.zoom_level * 100}%", color="black", anchor=(0, 0)
        )
        self.zoom_text.setPos(QPointF(80, 30))
        self.scene().addItem(self.zoom_text)
        
        # further defer update until the window is actually rendered to the screen
        #QApplication.processEvents()
        #QTimer.singleShot(0, self.update_plot)


    def resizeEvent(self, event) -> None:
        """
        Handles window resizing and updates compression indicator.
        """
        super().resizeEvent(event)
        if self.isVisible():
            self.update_plot()
        self.update_compression()

    def window_to_viewbox(self, point: QPointF) -> QPointF:
        """
        Converts between window (screen) coordinates and data (viewbox) coordinates.

        Parameters:
            point (QPointF): Point in global coordinates.

        Returns:
            QPointF: The corresponding point in data coordinates.
        """      
        scene_pos = self.mapToScene(point.toPoint())
        data_pos = self.viewbox.mapSceneToView(scene_pos)
        return data_pos

    def viewbox_to_window(self, point: QPointF) -> QPointF:
        """
        Converts between data (viewbox) coordinates and widget (screen) coordinates.

        Parameters:
            point (QPointF): Point in data coordinates.

        Returns:
            QPointF: The corresponding point in window coordinates.
        """
        return self.viewbox.mapViewToScene(point)
        # scene_pos = self.viewbox.mapViewToScene(QPointF(x, y))
        # return scene_pos.x(), scene_pos.y()
        # widget_pos = self.mapFromScene(scene_pos)
        # return widget_pos.x(), widget_pos.y()

    def reset_view(self) -> None:
        """
        Resets the plot to the full initial view, undoing all zoom/pan changes.
        """
        QGuiApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))

        self.xy_data = [
            self.initial_downsampled_data[0].copy(), 
            self.initial_downsampled_data[1].copy()
        ]

        self.curve.setData(self.xy_data[0], self.xy_data[1])

    
        x_min, x_max = self.xy_data[0][0], self.xy_data[0][-1]
        y_min, y_max = np.min(self.xy_data[1]), np.max(self.xy_data[1])

        self.viewbox.setRange(
            xRange=(x_min, x_max), 
            yRange=(y_min, y_max), 
            padding=0
        )

        QGuiApplication.restoreOverrideCursor()

    def update_plot(self) -> None:
        """
        Redraws the waveform curve and label overlays after zoom/pan/data change.
        Also updates compression and zoom indicators.
        """
        if self.file is None or self.file not in self.epgdata.dfs:
            return  # no file displayed yet

        (x_min, x_max), _ = self.viewbox.viewRange()

        self.viewbox.setLimits(xMin=None, xMax=None, yMin=None, yMax=None) # clear stale data (avoids warning)

        self.downsample_visible(x_range=(x_min, x_max))

        x_data = self.xy_data[0]
        y_data = self.xy_data[1]
        self.curve.setData(x_data, y_data)
        if len(x_data) <= 500:
            self.scatter.setVisible(True)
            self.scatter.setData(x_data, y_data)
        else:
            self.scatter.setVisible(False)

        self.update_compression()
        self.update_zoom()
        for label_area in self.labels:
            # Cull to visible labels
            if label_area.start_time + label_area.duration < x_min:
                continue
            if label_area.start_time > x_max:
                continue

            # Don't render label areas <1 px wide
            left_px_loc = self.viewbox_to_window(QPointF(label_area.start_time,0)).x()
            right_px_loc = self.viewbox_to_window(QPointF(label_area.start_time + label_area.duration, 0)).x()
            label_width_px = right_px_loc - left_px_loc

            if label_width_px < 1:
                label_area.setVisible(False)
                continue

            label_area.setVisible(True)
            label_area.update_label_area()

        self.viewbox.update()  # or anything that redraws

        if self.last_cursor_pos is not None:
            view_pos = self.window_to_viewbox(self.last_cursor_pos)
            self.selection.hover(view_pos.x(), view_pos.y())

            
        

    def update_compression(self) -> None:
        """
        Calculates the compression level based on the current zoom level.
        Uses a WinDAQ-derived formula.
        """
        # """
        # update_compression updates the compression readout
        # based on the zoom level according to the formula
        # COMPRESSION = (SECONDS/PIXEL) * 125
        # obtained by experimentation with WINDAQ. Note that
        # WINDAQ also has 'negative' compression levels for
        # high levels of zooming out. We do not implement those here.

        # TODO: Verify this formula
        # """

        if not self.isVisible():
            return  # don't run prior to initialization

        # Get the pixel distance of one second
        plot_width = self.viewbox.geometry().width() * self.devicePixelRatioF()

        (x_min, x_max), _ = self.viewbox.viewRange()
        time_span = x_max - x_min

        if time_span == 0:
            return float("inf")  # Avoid division by zero

        pix_per_second = plot_width / time_span
        second_per_pix = 1 / (pix_per_second)

        # Convert to compression based on WinDaq
        self.compression = second_per_pix * 125
        self.compression_text.setText(f"Compression Level: {self.compression :.1f}")

        # ----- CLINIC CODE -----------------
        # Update the compression readout.
        # Get the pixel distance of one second, we use a wide range to avoid rounding issues.
        # width = 1000
        # pix_per_second = (self.chart_to_window(width, 0)[0] - \
        #           self.chart_to_window(0, 0)[0]) / width
        # second_per_pix = 1 / (pix_per_second)
        # # Convert to compression based on WinDaq
        # self.compression = second_per_pix * 125
        # self.compression_text.setPlainText(f'Compression Level: {self.compression :.1f}')

    def update_zoom(self) -> None:
        """
        Updates the displayed zoom percentage based on current vs full-scale width.
        """
        plot_width = self.viewbox.geometry().width() * self.devicePixelRatioF()

        (x_min, x_max), _ = self.viewbox.viewRange()
        time_span = x_max - x_min

        pix_per_second = plot_width / time_span

        if time_span == 0:
            return float("inf")  # Avoid division by zero

        file_length_sec = self.epgdata.dfs[self.file]["time"].iloc[-1]
        default_pix_per_second = plot_width / file_length_sec

        self.zoom_level = pix_per_second / default_pix_per_second

        # leave off decimal if zoom level is int
        if abs(self.zoom_level - round(self.zoom_level)) < 1e-9:
            self.zoom_text.setText(f"Zoom: {self.zoom_level * 100: .0f}%")
        else:
            self.zoom_text.setText(f"Zoom: {self.zoom_level * 100: .1f}%")

    def plot_recording(self, file: str, prepost: str = "post") -> None:
        """
        Loads the time series and comments from a file and displays it.

        Parameters:
            file (str): File identifier.
            prepost (str): Either "pre" or "post" to select pre/post rectifier data.
        """
        QGuiApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))

        if self.labels: # clear previous labels, if any
            self.selection.deselect_all()
            for label_area in self.labels[::-1]:
                self.selection.delete_label_area(label_area, multi_delete=False)

        self.file = file
        self.prepost = prepost
        times, volts = self.epgdata.get_recording(self.file, self.prepost)

        self.xy_data[0] = times
        self.xy_data[1] = volts
        self.downsample_visible()
        self.curve.setData(self.xy_data[0], self.xy_data[1])
        init_x, init_y = self.xy_data[0].copy(), self.xy_data[1].copy()
        self.initial_downsampled_data = [init_x, init_y]
        df = self.epgdata.dfs[file]


        self.viewbox.setRange(
            xRange=(np.min(self.xy_data[0]), np.max(self.xy_data[0])), 
            yRange=(np.min(self.xy_data[1]), np.max(self.xy_data[1])), 
            padding=0
        )

        # create a comments column if doesn't yet exist in df
        if 'comments' not in df.columns:
            df['comments'] = None
        
        self.update_plot()
        self.plot_comments(file)
        QApplication.processEvents()
        QGuiApplication.restoreOverrideCursor()

    def plot_comments(self, file: str) -> None:
        """
        Adds existing comment markers from the data file to the viewbox.

        Parameters:
            file (str): File identifier.
        """
        if file is not None:
            self.file = file

        df = self.epgdata.dfs[self.file]

        for marker in self.comments:
            marker.remove()
        self.comments.clear()
        
        comments_df = df[~df["comments"].isnull()]
        for time, text in zip(comments_df["time"], comments_df["comments"]):
            marker = CommentMarker(time, text, self, icon_path=r"message.svg")
            self.comments[time] = marker
        
        return

    def add_comment(self, event: QMouseEvent) -> None:
        """
        Adds or edits a comment at the clicked location via a dialog popup.

        Parameters:
            event (QMouseEvent): The click event triggering comment placement.
        """
        point = self.window_to_viewbox(event.position())
        x = point.x()

        df = self.epgdata.dfs[self.file]
        # find nearest time clicked
        nearest_idx = (df['time'] - x).abs().idxmin()
        comment_time = float(df.at[nearest_idx, 'time'])
        existing = df.at[nearest_idx, 'comments']
        
        # if there's already a comment at the time clicked, give an option to replace
        if existing and str(existing).strip():
            confirm = QMessageBox.question(
                self,
                "Overwrite Comment?",
                f"A comment already exists at {df.at[nearest_idx, 'time']:.2f}s:\n\n\"{existing}\"\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
            if confirm == QMessageBox.StandardButton.No:
                return

        # Create the dialog popup
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Add Comment @ {comment_time:.2f}s")
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Add Comment:"))
        text = QTextEdit()
        layout.addWidget(text)

        save_button = QPushButton("Save")
        cancel_button = QPushButton("Cancel")
        layout.addWidget(save_button)
        layout.addWidget(cancel_button)
        save_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        # if the text was just spaces/an empty comment, then don't create a comment
        text = text.toPlainText().strip()
        if not text:
            return
    
        # create comment
        df.at[nearest_idx, 'comments'] = text
        marker = self.comments.get(comment_time)
        if marker:
            marker.set_text(text)
        else:
            new_marker = CommentMarker(comment_time, text, self)
            self.comments[comment_time] = new_marker

        self.comment_preview_enabled = False
        self.comment_preview.setVisible(False)
        return

    def delete_comment(self, time: float) -> None:
        # update df
        df = self.epgdata.dfs[self.file]
        df.loc[df["time"] == time, "comments"] = None

        # update dict
        marker = self.comments.pop(time)
        marker.remove()
        return

    def move_comment_helper(self, marker: CommentMarker):
        self.moving_comment = marker
        self.comment_preview_enabled = True
        self.comment_preview.setVisible(True)

        x_pos = self.viewbox.mapSceneToView(self.mapToScene(self.mapFromGlobal(QCursor.pos()))).x()
        self.comment_preview.setPos(x_pos)
        
        self.selection.deselect_all()
        self.selection.unhighlight_item(self.selection.hovered_item)
        self.viewbox.update()
        return
    
    def move_comment(self, marker: CommentMarker, time: float) -> None:
        df = self.epgdata.dfs[self.file]
        new_idx = (df['time'] - time).abs().idxmin()
        new_time = float(df.at[new_idx, 'time'])
        old_time = marker.time
        text = self.comments[old_time].text

        # update df
        df.loc[df['time'] == old_time, 'comments'] = None
        df.at[new_idx, 'comments'] = text

        # update comments dict
        old_marker = self.comments.pop(old_time)
        old_marker.remove()
        new_marker = CommentMarker(new_time, text, self)
        self.comments[new_time] = new_marker

        self.comment_preview_enabled = False
        self.comment_preview.setVisible(False)
        return
        
    def downsample_visible(
        self, x_range: tuple[float, float] = None, max_points=4000, method = 'peak'
    ) -> tuple[NDArray, NDArray]:
        """
        Downsamples waveform data in the visible range using the selected method. Modifies self.xy_data in-place.

        Parameters:
            x_range (tuple[float, float]): Optional x-axis range to downsample.
            max_points (int): Max number of points to plot.
            method (str): 'subsample', 'mean', or 'peak' downsampling method.
        
        NOTE: 
            `subsample` samples the first point of each bin (fastest)
            `mean` averages each bin
            `peak` returns the min and max point of each bin (slowest, best looking)
        """
        x, y = self.epgdata.get_recording(self.file, self.prepost)

        # Filter to x_range if provided
        if x_range is not None:
            x_min, x_max = x_range

            left_idx = np.searchsorted(x, x_min, side="left")
            right_idx = np.searchsorted(x, x_max, side="right")

            if right_idx - left_idx <= 250: 
                # render additional point on each side at very high zooms
                left_idx = max(0, left_idx - 1)
                right_idx = min(len(x), right_idx + 1)
  
            x = x[left_idx:right_idx]
            y = y[left_idx:right_idx]   
    
        num_points = len(x)

        if num_points <= max_points or num_points < 2:  # no downsampling needed
            self.xy_data[0] = x
            self.xy_data[1] = y
            return

        if method == 'subsampling': 
            stride = num_points // max_points
            x_out = x[::stride]
            y_out = y[::stride]
        elif method == 'mean':
            stride = num_points // max_points
            num_windows = num_points // stride
            start_idx = stride // 2
            x_out = x[start_idx : start_idx + num_windows * stride : stride] 
            y_out = y[:num_windows * stride].reshape(num_windows,stride).mean(axis=1)
        elif method == 'peak':
            stride = max(1, num_points // (max_points // 2))  # each window gives 2 points
            num_windows = num_points // stride

            start_idx = stride // 2  # Choose a representative x (near center) for each window
            x_win = x[start_idx : start_idx + num_windows * stride : stride]
            x_out = np.repeat(x_win, 2)  # repeated for (x, y_min), (x, y_max)

            y_reshaped = y[: num_windows * stride].reshape(num_windows, stride)
            y_out = np.empty(num_windows * 2)
            y_out[::2] = y_reshaped.max(axis=1)
            y_out[1::2] = y_reshaped.min(axis=1)
        else:
            raise ValueError(
                'Invalid "method" arugment. ' \
                'Please select either "subsampling", "mean", or "peak".'
            )

        self.xy_data[0] = x_out
        self.xy_data[1] = y_out

    def plot_transitions(self, file: str) -> None:
        """
        Plots labeled regions from label transition data as colored areas on the plot.

        Also inserts a zero-width "END AREA" to add the final transition line.

        Parameters:
            file (str): File identifier.
        """
        # clear old labels if present
        for label_area in self.labels:
            self.plot_item.removeItem(label_area.area)
            self.plot_item.removeItem(label_area.transition_line)
            self.plot_item.removeItem(label_area.label_text)
            self.plot_item.removeItem(label_area.label_background)
            self.plot_item.removeItem(label_area.duration_text)
            self.plot_item.removeItem(label_area.duration_background)
            if self.enable_debug:
                self.plot_item.removeItem(label_area.label_debug_box)
                self.plot_item.removeItem(label_area.duration_debug_box)
            
        self.labels = []

        # load data
        times, _ = self.epgdata.get_recording(self.file, self.prepost)
        transitions = self.epgdata.get_transitions(self.file, self.transition_mode)

        # only continue if the label column contains labels
        if self.epgdata.dfs[file][self.transition_mode].isna().all():
            return
        
        durations = []  # elements of (label_start_time, label_duration, label)
        for i in range(len(transitions) - 1):
            time, label = transitions[i]
            next_time, _ = transitions[i + 1]
            durations.append((time, next_time - time, label))
        durations.append((transitions[-1][0], max(times) - transitions[-1][0], transitions[-1][1]))

        for i, (time, dur, label) in enumerate(durations):
            if label == None:
                continue
            label_area = LabelArea(time, dur, label, self) # init. also adds items to viewbox
            self.labels.append(label_area)

        # NOTE: Each LabelArea has one transition line, but we need an additional ending
        # line so that the final LabelArea doesn't end without a transition line. We chose
        # to do this by adding a zero-width, invisible LabelArea, called the "end area", to 
        # the end of the labels, so that just the transition line appears.
        time, dur, _ = durations[-1]
        end_start_time = time + dur

        label_area = LabelArea(end_start_time, 0, 'END AREA', self)
        label_area.label_text.setVisible(False)
        label_area.label_background.setVisible(False)
        label_area.duration_text.setVisible(False)
        label_area.duration_background.setVisible(False)
        self.labels.append(label_area)
        
        self.update_plot()

    def change_label_color(self, label: str, color: QColor) -> None:
        """
        Updates all label regions with the specified label to the new background color.

        Parameters:
            label (str): Label type to update.
            color (QColor): Color to set the label areas to.
        """
        for label_area in self.labels:
            if label_area.label == label:
                label_area.area.setBrush(mkBrush(color))
                label_area.update_label_area()

    def change_line_color(self, color: QColor) -> None:
        """
        Changes the waveform curve and scatter plot line color.

        Parameters:
            color (QColor): Color to set the curve and scatter plot to.
        """
        self.curve.setPen(mkPen(color))
        self.scatter.setPen(mkPen(color))  

    def set_durations_visible(self, visible: bool):
        """
        Sets the visibility of all label area durations.

        Parameters:
            visible (bool): Whether to show or hide the durations.
        """
        for label_area in self.labels:
            if label_area.is_end_area:
                continue
            label_area.set_duration_visible(visible)
         

    def composite_on_white(self, color: QColor) -> QColor:
        """
        Helper function to convert a color with alpha into a RGB (no A) color as if shown on white.

        Parameters:
            color (QColor): Semi-transparent color to composite with white. 
        """
        r, g, b, a = color.getRgb()
        a = a / 255

        new_r = round(r * a + 255 * (1 - a))
        new_g = round(g * a + 255 * (1 - a))
        new_b = round(b * a + 255 * (1- a))
        return QColor(new_r, new_g, new_b)
        
    def get_closest_transition(self, x: float) -> tuple[int, float]:
        """
        Finds the transition line closest to the given x-coordinate.

        Parameters:
            x (float): ViewBox x-coordinate.
        Returns:
            (InfiniteLine, float): Closest transition line and pixel distance.
        """
        if not self.labels:
            return None, float('inf')  # no labels present
        
        transitions = np.array([label_area.start_time for label_area in self.labels])
        idx = np.searchsorted(transitions, x)

        zero_point = self.viewbox_to_window(QPointF(0,0)).x()
        
        if idx == len(transitions):
            transition = self.labels[idx-1].transition_line
            dist = abs(transitions[idx-1] - x)
            dist_px = self.viewbox_to_window(QPointF(dist, 0)).x() - zero_point
            return transition, dist_px
        
        else:
            # Check which of the two neighbors is closer
            dist_to_left = abs(x - transitions[idx-1])
            dist_to_right = abs(transitions[idx] - x)

            if dist_to_left <= dist_to_right:
                transition = self.labels[idx-1].transition_line
                dist_px = self.viewbox_to_window(QPointF(dist_to_left, 0)).x()- zero_point
                return transition, dist_px
            else:
                transition = self.labels[idx].transition_line
                dist_px = self.viewbox_to_window(QPointF(dist_to_right, 0)).x()- zero_point
                return transition, dist_px
            
    def get_baseline_distance(self, y: float) -> float:
        """
        Returns the baseline and the pixel distance to it from a y-coordinate.

        Parameters:
            y (float): ViewBox y-coordinate.
        Returns:
            (InfiniteLine, float): Baseline and pixel distance.
        """
        if self.baseline is None:
            return float('inf')
        zero_point = self.viewbox_to_window(QPointF(0,0)).y()
        viewbox_distance = abs(y - self.baseline.value())
        return self.baseline, zero_point - self.viewbox_to_window(QPointF(0, viewbox_distance)).y()
    
    def get_closest_label_area(self, x: float) -> LabelArea:
        """
        Returns the LabelArea covering the given x position, or None if out of bounds.

        Parameters:
            x (float): ViewBox x-coordinate.
        Returns:
            LabelArea: Label area the x-coordinate is part of.
        """

        if not self.labels:
            return None
        
        # don't include the last label
        visible_labels = [label for label in self.labels if not label == self.labels[-1]]

        if not visible_labels:
            return None

        if x < visible_labels[0].start_time or x > (visible_labels[-1].start_time + visible_labels[-1].duration):
            return None  # outside the labels
        label_ends = np.array([label.start_time + label.duration for label in visible_labels])
        idx = np.searchsorted(label_ends, x)  # idk why this works
        if idx >= len(visible_labels):
            return visible_labels[-1]
        return visible_labels[idx]

    def delete_all_label_instances(self, label: str) -> None:
        """
        TODO: implement
        """

    def handle_transitions(self):
        return

    def handle_labels(self):
        return

    def set_baseline(self, event: QMouseEvent):
        """
        Sets a baseline at the clicked y-position and shows the baseline.

        Parameters:
            event (QMouseEvent): The click event triggering baseline placement.
        """
        # TODO: edit for when have edit mode functionality
        point = self.window_to_viewbox(event.position())
        x, y = point.x(), point.y()

        (x_min, x_max), (y_min, y_max) = self.viewbox.viewRange()

        if not (x_min <= x <= x_max and y_min <= y <= y_max):
            return

        
        self.baseline.setPos(y)
        self.baseline.setVisible(True)

        self.baseline_preview_enabled = False
        self.baseline_preview.setVisible(False)


    def add_drop_transitions(self):
        return

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """
        Handles key shortcuts for setting baseline ("B") or comment ("C").
        Also forwards key events to the selection manager.

        Parameters:
            event (QKeyEvent): The key press event.
        """
        if event.key() == Qt.Key.Key_R:
            self.reset_view()  
        if event.key() == Qt.Key.Key_B:
            if self.baseline_preview_enabled:
                # Turn it off
                self.baseline_preview_enabled = False
                self.baseline_preview.setVisible(False)
            else:
                # disable simultaneous "c" click 
                self.comment_preview_enabled = False
                self.comment_preview.setVisible(False)
                # prepare to set baseline
                self.baseline.setVisible(False)
                self.baseline_preview_enabled = True
                self.baseline_preview.setVisible(True)
                y_pos = self.viewbox.mapSceneToView(self.mapToScene(self.mapFromGlobal(QCursor.pos()))).y()
                self.baseline_preview.setPos(y_pos)
            self.selection.deselect_all()
            self.selection.unhighlight_item(self.selection.hovered_item)
        if event.key() == Qt.Key.Key_C:
            if self.comment_preview_enabled:
                # Turn it off
                self.comment_preview_enabled = False
                self.comment_preview.setVisible(False)
            else:
                # disable simultaneous "b" click 
                self.baseline_preview_enabled = False
                self.baseline_preview.setVisible(False)
                # prepare to add new comment
                self.comment_preview_enabled = True
                self.comment_preview.setVisible(True)
                x_pos = self.viewbox.mapSceneToView(self.mapToScene(self.mapFromGlobal(QCursor.pos()))).x()
                self.comment_preview.setPos(x_pos)
            self.selection.deselect_all()
            self.selection.unhighlight_item(self.selection.hovered_item)

        self.selection.key_press_event(event)
        self.viewbox.update()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        return
        if event.key() == Qt.Key.Key_Shift:
            self.vertical_mode = False
        elif event.key() == Qt.Key.Key_Control:
            self.zoom_mode = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """
        Delegates interaction to selection, baseline, and comment handlers based on state.

        Parameters:
            event (QMouseEvent): The mouse click event.
        """
        super().mousePressEvent(event)

        point = self.window_to_viewbox(event.position())
        x, y = point.x(), point.y()

        if event.button() == Qt.MouseButton.LeftButton:
            if self.baseline_preview_enabled:
                self.set_baseline(event)
            elif self.comment_preview_enabled and self.moving_comment is not None:
                new_time = x
                self.move_comment(self.moving_comment, new_time)
                self.moving_comment = None
            elif self.comment_preview_enabled:
                self.add_comment(event)
            else:
                self.selection.mouse_press_event(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """
        Delegates interaction to selection, baseline, and comment handlers based on state.
        Parameters:
            event (QMouseEvent): The mouse release event.
        """

        super().mouseReleaseEvent(event)

        self.selection.mouse_release_event(event)

        if self.moving_mode:
            # if transition line was released, update data transition line
            if isinstance(self.selected_item, InfiniteLine) and self.selected_item is not self.baseline:
                transitions = [(label_area.start_time, label_area.label) for label_area in self.labels]
                self.epgdata.set_transitions(self.file, transitions, self.transition_mode)
        return
        if event.button() == Qt.MouseButton.LeftButton:
            self.handle_transitions(event, "release")

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        return
        if event.button() == Qt.MouseButton.LeftButton:
            self.handle_labels(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """
        Delegates interaction to selection, baseline, and comment handlers based on state.

        Parameters:
            event (QMouseEvent): The mouse move event.
        """
        super().mouseMoveEvent(event)
        self.last_cursor_pos

        point = self.window_to_viewbox(event.position())
        x, y = point.x(), point.y()

        (x_min, x_max), (y_min, y_max) = self.viewbox.viewRange()

        if self.baseline_preview_enabled:
            if y_min <= y <= y_max:
                self.baseline_preview.setPos(y)
                self.baseline_preview.setVisible(True)
            else:
                self.baseline_preview.setVisible(False)
        elif self.comment_preview_enabled:
            if x_min <= x <= x_max:
                self.comment_preview.setPos(x)
                self.comment_preview.setVisible(True)
            else:
                self.comment_preview.setVisible(False)
        else:
            self.selection.mouse_move_event(event)

        return

        self.handle_transitions(event, "move")

    

    def wheelEvent(self, event: QWheelEvent) -> None:
        """
        Forwards a scroll event to the custom viewbox.
        """
        self.viewbox.wheelEvent(event)
        event.ignore()


# TODO: remove after feature-complete and integrated with main
def main():
    Settings()
    
    app = QApplication([])

    epgdata = EPGData()
    epgdata.load_data(r"C:\EPG-Project\Summer\CS-Repository\Exploration\Jonathan\Data\sharpshooter_label2.csv")
    #epgdata.load_data("test_recording.csv")

    #epgdata.load_data(r'C:\EPG-Project\Summer\CS-Repository\Exploration\Jonathan\Data\smooth_18mil.csv')
    window = DataWindow(epgdata)
    window.plot_recording(window.epgdata.current_file, 'post')
    window.plot_transitions(window.epgdata.current_file)

    window.showMaximized()

    
    

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

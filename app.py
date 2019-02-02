import os
import re
import gc
import time
import random
from shutil import rmtree
import multiprocessing
from datetime import timedelta
from functools import update_wrapper

import dlib
import numpy as np
import cv2
from PIL import Image, ExifTags
from scipy.spatial import distance
from flask import Flask, render_template, request, send_file, url_for

from face_swap import warp_image_2d, warp_image_3d, mask_from_points, \
                      apply_mask, correct_colours, transformation_from_points
#from utils import *

def open_img(img, biggest=400):
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    scale = biggest / max(img.shape[:-1]) 
    img = cv2.resize(img, (int(img.shape[1]*scale), int(img.shape[0]*scale)), Image.LANCZOS)
    return img

def read_file_buffer(f):
    img = f.read()
    img = np.fromstring(img, np.uint8)
    return cv2.imdecode(img, cv2.IMREAD_COLOR)

def face_detection(img):
    # Ask the detector to find the bounding boxes of each face. The 1 in the
    # second argument indicates that we should upsample the image 1 time. This
    # will make everything bigger and allow us to detect more faces.
    detector = dlib.get_frontal_face_detector()
    faces = detector(img, 1)
    bboxs = []

    for face in faces:
        bboxs.append((face.left(), face.top(), face.right(), face.bottom()))
    
    return bboxs

def face_points_detection(img, bbox):
    # Get the landmarks/parts for the face in box d.
    shape = PREDICTOR(img, bbox)

    # loop over the 68 facial landmarks and convert them
    # to a 2-tuple of (x, y)-coordinates
    coords = [(shape.part(i).x, shape.part(i).y) for i in range(68)]

    # return the list of (x, y)-coordinates
    return coords

def select_faces(im, bbox, r=10):
    points = face_points_detection(im, dlib.rectangle(*bbox))
    im_w, im_h = im.shape[:2]
    left, top = np.min(points, 0)
    right, bottom = np.max(points, 0)
    
    x, y = max(0, left-r), max(0, top-r)
    w, h = min(right+r, im_h)-x, min(bottom+r, im_w)-y

    return points - np.asarray([[x, y]]), (x, y, w, h), im[y:y+h, x:x+w]

def calc_dist(img0, img1):
    return distance.euclidean(img0, img1)

def get_selfie_bboxs(me):
    my_bboxs = np.array(face_detection(me[0]))
    # points on the faces to be swapped (selfies / friends photos)
    if me[1] is not None:
        to_keep = []
        for arg in me[1]:
            for j, bbox in enumerate(my_bboxs):
                if (bbox[0] <= arg[0] <= bbox[2]) and \
                   (bbox[1] <= arg[1] <= bbox[3]):
                        to_keep.append(j)
        my_bboxs = my_bboxs[to_keep]
    return my_bboxs

def chunker(l, n):
    length = len(l)
    n = length // n
    for i in range(0, length, n):
        if length - i < 2*n:
            yield l[i:]
            break
        else:
            yield l[i:i + n]

class preprocess_img:

    @classmethod
    def run(self, CROWD, SELFIES, max_dst_boxes=25, embeddings_max_iters=2, n_jobs=2):
        #args is a list of tuples: (img, points)
       
        self.CROWD = CROWD 
        self.embeddings_max_iters = embeddings_max_iters

        selfies_boxes = []
        for SELFIE in SELFIES:
            selfies_boxes.append(get_selfie_bboxs(SELFIE))

        selfies_boxes_len = sum([len(my_bboxs) for my_bboxs in selfies_boxes])
        self.bboxs = np.array(face_detection(CROWD))
        if len(self.bboxs) == 0 or selfies_boxes_len == 0:
            return None

        random_sample_bboxs = np.random.choice(list(range(len(self.bboxs))), 
                                               size=min(len(self.bboxs), max_dst_boxes))
        random_sample_my_bboxs = np.random.choice(list(range(selfies_boxes_len)), 
                                               size=min(selfies_boxes_len, max_dst_boxes))

        if len(random_sample_bboxs) < len(self.bboxs):
            self.bboxs = self.bboxs[random_sample_bboxs]

        if len(random_sample_my_bboxs) < selfies_boxes_len:
            my_bboxs = my_bboxs[random_sample_my_bboxs]

        if len(self.bboxs) < 2:
            n_jobs = 1
            
        out, self.ignore_list = [], []
        for img_num, SELFIE in enumerate(SELFIES):
            my_bboxs = selfies_boxes[img_num]
            for i in range(len(my_bboxs)):
                self.src_face_descriptor = FACEREC.compute_face_descriptor(SELFIE[0], 
                                      SP(SELFIE[0], dlib.rectangle(*my_bboxs[i])), embeddings_max_iters)

                if n_jobs == 1:
                    dsts = np.full(len(self.bboxs), np.inf)
                    for j, bbox in enumerate(self.bboxs):
                        if j in self.ignore_list:
                            continue
                        face_descriptor = FACEREC.compute_face_descriptor(CROWD,
                                          SP(CROWD, dlib.rectangle(*bbox)), embeddings_max_iters)
                        dsts[j] = calc_dist(src_face_descriptor, face_descriptor)
                else:
                    manager = multiprocessing.Manager()
                    pool = multiprocessing.Pool(n_jobs)
                    res = manager.list([0] * n_jobs)
                    chunks = list(chunker(list(range(len(self.bboxs))), n_jobs))
                    pool.map(self.run_comparison, [(j, chunks[j], res) for j in range(n_jobs)])
                    dsts = np.concatenate([res[j] for j in range(n_jobs)], axis=0)
                    pool.close()
                    gc.collect()

                clst = np.argmin(dsts)
                self.ignore_list.append(clst)
                out.append((select_faces(CROWD, self.bboxs[clst]), select_faces(SELFIE[0], my_bboxs[i])))
        return out
    
    @classmethod
    def run_comparison(self, args):
        k, chunk, res = args
        dsts = np.full(len(chunk), np.inf)
        for l, m in enumerate(chunk):
            if m in self.ignore_list:
                continue
            face_descriptor = FACEREC.compute_face_descriptor(self.CROWD,
                              SP(self.CROWD, dlib.rectangle(*self.bboxs[m])), self.embeddings_max_iters)
            dsts[l] = calc_dist(self.src_face_descriptor, face_descriptor)
        res[k] = dsts
        del dsts; gc.collect()

def insert_face(result, CROWD):
    
    if result is None:
        return None
 
    result_bboxs = []
    for faces in result:
        dst_points, dst_shape, dst_face = faces[0]
        src_points, src_shape, src_face = faces[1]

        w, h = dst_face.shape[:2]

        ### Warp Image
        if not WARP_2D:
            ## 3d warp
            warped_src_face = warp_image_3d(src_face, src_points[:MAX_POINTS], dst_points[:MAX_POINTS], (w, h))
        else:
            ## 2d warp
            src_mask = mask_from_points(src_face.shape[:2], src_points, radius=2)
            src_face = apply_mask(src_face, src_mask)
            # Correct Color for 2d warp
            if CORRECT_COLOR:
                warped_dst_img = warp_image_3d(dst_face, dst_points[:MAX_POINTS], 
                                               src_points[:MAX_POINTS], src_face.shape[:2])
                src_face = correct_colours(warped_dst_img, src_face, src_points)
            # Warp
            warped_src_face = warp_image_2d(src_face, transformation_from_points(dst_points, src_points), (w, h, 3))

        ## Mask for blending
        mask = mask_from_points((w, h), dst_points, radius=2)
        mask_src = np.mean(warped_src_face, axis=2) > 0
        mask = np.asarray(mask*mask_src, dtype=np.uint8)

        ## Correct color
        if not WARP_2D and CORRECT_COLOR:
            warped_src_face = apply_mask(warped_src_face, mask)
            dst_face_masked = apply_mask(dst_face, mask)
            warped_src_face = correct_colours(dst_face_masked, warped_src_face, dst_points)

        ##Poisson Blending
        r = cv2.boundingRect(mask)
        center = ((r[0] + int(r[2] / 2), r[1] + int(r[3] / 2)))
        output = cv2.seamlessClone(warped_src_face, dst_face, mask, center, cv2.NORMAL_CLONE)

        x, y, w, h = dst_shape
        result_bboxs.append(dst_shape)
        CROWD[y:y+h, x:x+w] = output
        

    output_labeled = CROWD.copy()
    for bbox in result_bboxs:
        x, y, w, h = bbox
        cv2.rectangle(output_labeled, (x, y), (x+w, y+h), (255,0,0), 2)
    
    return output_labeled

def crossdomain(origin=None, methods=None, headers=None,
                max_age=21600, attach_to_all=True,
                automatic_options=True):
    if methods is not None:
        methods = ', '.join(sorted(x.upper() for x in methods))
    if headers is not None and not isinstance(headers, str):
        headers = ', '.join(x.upper() for x in headers)
    if not isinstance(origin, str):
        origin = ', '.join(origin)
    if isinstance(max_age, timedelta):
        max_age = max_age.total_seconds()

    def get_methods():
        if methods is not None:
            return methods

        options_resp = current_app.make_default_options_response()
        return options_resp.headers['allow']

    def decorator(f):
        def wrapped_function(*args, **kwargs):
            if automatic_options and request.method == 'OPTIONS':
                resp = current_app.make_default_options_response()
            else:
                resp = make_response(f(*args, **kwargs))
            if not attach_to_all and request.method != 'OPTIONS':
                return resp

            h = resp.headers
            h['Access-Control-Allow-Origin'] = origin
            h['Access-Control-Allow-Methods'] = get_methods()
            h['Access-Control-Max-Age'] = str(max_age)
            h['Access-Control-Allow-Credentials'] = 'true'
            h['Access-Control-Allow-Headers'] = \
                "Origin, X-Requested-With, Content-Type, Accept, Authorization"
            if headers is not None:
                h['Access-Control-Allow-Headers'] = headers
            return resp

        f.provide_automatic_options = False
        #f.required_methods = ['OPTIONS']
        return update_wrapper(wrapped_function, f)
    return decorator

#load trained models
# face landmarks
PREDICTOR = dlib.shape_predictor('./models/shape_predictor_68_face_landmarks.dat')
# dlib face recognition
SP = dlib.shape_predictor("./models/shape_predictor_5_face_landmarks.dat")
FACEREC = dlib.face_recognition_model_v1("./models/dlib_face_recognition_resnet_model_v1.dat")

MAX_SIZE_SELFIE = 400 
MAX_SIZE_CROWD = 1000
WARP_2D = False
CORRECT_COLOR = True
MAX_POINTS = 58

app = Flask(__name__)
app.debug=True
app.config['UPLOAD_FOLDER'] = os.path.basename('static')

print(" [INFO] Server loaded! ")

@app.route('/')
def hello_world():
    return render_template('index.html')

def read_image_exif(stream):
    image=Image.open(stream)
    try: 
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation]=='Orientation':
                break
        exif=dict(image._getexif().items())

        if exif[orientation] == 3:
            image=image.rotate(180, expand=True)
        elif exif[orientation] == 6:
            image=image.rotate(270, expand=True)
        elif exif[orientation] == 8:
            image=image.rotate(90, expand=True)

    except (AttributeError, KeyError, IndexError):
        # cases: image don't have getexif
        pass

    # convert to opencv format
    cv_image = np.array(image.convert('RGB'))
    image.close()
    cv_image = cv_image[:, :, ::-1].copy()
    return cv_image


@app.route('/create_mix',  methods=['GET', 'POST'])
def upload_create_mix():

    CROWD = read_image_exif(request.files['image_crowd'].stream)
    ME = read_image_exif(request.files['image_selfie'].stream)

    start = time.time() 
    ME = open_img(ME, biggest=MAX_SIZE_SELFIE)
    print(f" [INFO] Selfie shape: {ME.shape}")

    old_shape = CROWD.shape[:-1][::-1]
    CROWD = open_img(CROWD, biggest=MAX_SIZE_CROWD) 
    print(f" [INFO] Crowd shape: {CROWD.shape}")

    #MIX
    #preprocess_img gets one photo of the crowd and a list of (selfies, points) 
    result = preprocess_img.run(CROWD, [(ME, None)])
    output_labeled = insert_face(result, CROWD)
    if output_labeled is None:
        print(" [INFO] Something goes wrong :( ")
        return render_template('index.html', created_success=False, init=True)

    CROWD = cv2.resize(CROWD, old_shape, Image.LANCZOS)
    output_labeled = cv2.resize(output_labeled, old_shape, Image.LANCZOS)

    print(f" [INFO] Time consumed:  {int((time.time() - start) * 1000)} ms. ")

    file = os.path.join(app.config['UPLOAD_FOLDER'])
    if not os.path.exists(file):
        os.makedirs(file)
    # save answer 
    cv2.imwrite(file + "/result.jpeg", cv2.cvtColor(CROWD, cv2.COLOR_RGB2BGR))
    # save labeled answer
    cv2.imwrite(file + "/answer.jpeg", cv2.cvtColor(output_labeled, cv2.COLOR_RGB2BGR))

    result_filename = url_for('static', filename='result.jpeg') + '?rnd=' + str(random.randint(0, 10e9))
    answer_filename = url_for('static', filename='answer.jpeg') + '?rnd=' + str(random.randint(0, 10e9))
    return render_template('index.html', created_success=True, init=True,
                           result_filename=result_filename, answer_filename=answer_filename)

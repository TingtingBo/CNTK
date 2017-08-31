# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
#
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import os, sys
from cntk import Axis, load_model
from cntk.io import MinibatchSource, ImageDeserializer, CTFDeserializer, StreamDefs, StreamDef
from cntk.io.transforms import scale
from cntk.ops import input_variable
from cntk.logging import TraceLevel
import numpy as np

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(abs_path, ".."))
from utils.map_helpers import evaluate_detections
from utils.hierarchical_classification.hierarchical_classification_helper import HierarchyHelper
from utils.hierarchical_classification.htree_helper import get_tree_str

from utils.rpn.bbox_transform import regress_rois
USE_HIERARCHICAL_CLASSIFICATION = True
# TODO: make HCH a parameter to the evaluation
datasetName = "Grocery"
HCH = HierarchyHelper(get_tree_str(datasetName, USE_HIERARCHICAL_CLASSIFICATION))
HCH.tree_map.root_node.print()

use_gt_as_rois = False

def prepare_ground_truth_boxes(gtbs):
    """
    Creates an object that can be passed as the parameter "all_gt_infos" to "evaluate_detections" in map_helpers
    Parameters
    ----------
    gtbs - arraylike of shape (nr_of_images, nr_of_boxes, cords+original_label) where nr_of_boxes may be a dynamic axis

    Returns
    -------
    Object for parameter "all_gt_infos"
    """
    num_test_images = len(gtbs)
    classes = HCH.output_mapper.get_all_classes()  # list of classes with new labels and indexing
    all_gt_infos = {key: [] for key in classes}
    for image_i in range(num_test_images):
        image_gtbs = np.copy(gtbs[image_i])
        coords = image_gtbs[:, 0:4]
        original_labels = image_gtbs[:, -1:]

        all_gt_boxes = []
        for gtb_i in range(len(image_gtbs)):
            label = int(original_labels[gtb_i][0])
            train_vector, _ = HCH.get_vectors_for_label_nr(label)
            reduced_vector = HCH.output_mapper.get_prediciton_vector(train_vector)  # remove lower backgrounds

            original_cls_name = HCH.cls_maps[0].getClass(label)
            for vector_i in range(1, len(reduced_vector)):
                if reduced_vector[vector_i] == 0: continue
                # else this label (vector_i) is active (either original or hypernym)

                current_class_name = classes[vector_i]
                if original_cls_name == current_class_name: original_cls_name = None

                lbox = np.concatenate([coords[gtb_i], [vector_i]], axis=0)
                lbox.shape = (1,) + lbox.shape
                all_gt_boxes.append(lbox)

            assert original_cls_name is None, "Original class label is not contained in mapped selection!"

        all_gt_boxes = np.concatenate(all_gt_boxes, axis=0)

        for cls_index, cls_name in enumerate(classes):
            if cls_index == 0: continue
            cls_gt_boxes = all_gt_boxes[np.where(all_gt_boxes[:, -1] == cls_index)]
            all_gt_infos[cls_name].append({'bbox': np.array(cls_gt_boxes),
                                           'difficult': [False] * len(cls_gt_boxes),
                                           'det': [False] * len(cls_gt_boxes)})

    return all_gt_infos


def prepare_predictions(outputs, roiss, num_classes):
    """
    prepares the prediction for the ap computation.
    :param outputs: list of outputs per Image of the network
    :param roiss: list of rois rewponsible for the predictions of above outputs.
    :param num_classes: the total number of classes
    :return: Prepared object for ap computation by utils.map.map_helpers
    """
    num_test_images = len(outputs)

    all_boxes = [[[] for _ in range(num_test_images)] for _ in range(num_classes)]

    for img_i in range(num_test_images):
        output = outputs[img_i]
        #import pdb; pdb.set_trace()
        #num_rois = outpus.shape[0] /
        #output.shape = output.shape[1:]
        rois = roiss[img_i]

        preds_for_img = []
        for roi_i in range(len(output)):
            pred_vector = output[roi_i]
            roi = rois[roi_i]

            processesed_vector = HCH.top_down_eval(pred_vector)
            reduced_p_vector = HCH.output_mapper.get_prediciton_vector(processesed_vector)

            assert len(reduced_p_vector) == num_classes
            for label_i in range(num_classes):
                if (reduced_p_vector[label_i] == 0): continue
                prediciton = np.concatenate([roi, [reduced_p_vector[label_i], label_i]])  # coords+score+label
                prediciton.shape = (1,) + prediciton.shape
                preds_for_img.append(prediciton)

        preds_for_img = np.concatenate(preds_for_img, axis=0)  # (nr_of_rois x 6) --> coords_score_label

        for cls_j in range(1, num_classes):
            coords_score_label_for_cls = preds_for_img[np.where(preds_for_img[:, -1] == cls_j)]
            all_boxes[cls_j][img_i] = coords_score_label_for_cls[:, :-1].astype(np.float32, copy=False)

    return all_boxes

def to_image_input_coordinates(coords, img_dims=None, relative_coord=False, centered_coords=False, img_input_dims=None,
                               needs_padding_adaption=True, is_absolute=False):
    """
    Converts the input coordinates to the coordinates type required for the prediction
    :param coords: the coordinates to be transformed
    :param img_dims: dimension of the image the coords are from. Not required if coordinates are not needing any adaption for the padding and are relative.
    :param relative_coord: whether the supplied coordinates are relative
    :param centered_coords: whether the supplied coordinates are True:(center_x, center_y, width, heigth) or False:(left_bound, top_bound, right_bound, bottom_vound)
    :param img_input_dims: dimension of the detectors image input as tuple
    :param needs_padding_adaption: whether or not padding is used.
    :param is_absolute: whether or not the coordinate are absolute coordinates on the original image
    :return: transformed coords
    """
    if centered_coords:
        xy = coords[:, :2]
        wh_half = coords[:, 2:] / 2
        coords = np.concatenate([xy - wh_half, xy + wh_half], axis=1)

    # make coords relative
    if is_absolute:
        coords /= (img_dims[0], img_dims[1], img_dims[0], img_dims[1])
        relative_coord = True

    # applies padding transformation if required - restricted to sqare sized image inputs
    if needs_padding_adaption and img_dims is not None:
        if img_dims[0] > img_dims[1]:
            coords[:, [1, 3]] *= img_dims[1] / img_dims[0]
            coords[:, [1, 3]] += (1 - img_dims[1] / img_dims[0]) / 2
        elif img_dims[0] < img_dims[1]:
            coords[:, [0, 2]] *= img_dims[0] / img_dims[1]
            coords[:, [0, 2]] += (1 - img_dims[0] / img_dims[1]) / 2

    if relative_coord:
        coords *= img_input_dims + img_input_dims

    return coords


def eval_fast_rcnn_mAP(frcn_eval, cfg, minibatch_source, input_map):
    classes = HCH.output_mapper.get_all_classes()
    num_test_images = 10 # cfg.DATA.NUM_TEST_IMAGES
    num_classes = len(classes)

    all_raw_gt_boxes = []
    all_raw_outputs = []
    all_raw_rois = []

    # evaluate test images and write network output to file
    print("Evaluating Faster R-CNN model for %s images." % num_test_images)
    print(type(classes))
    for img_i in range(0, num_test_images):
        mb_data = minibatch_source.next_minibatch(1, input_map=input_map)

        # receives rel coords
        gt_data = mb_data[input_map[minibatch_source.roi_si]].asarray()
        gt_data = gt_data.reshape((cfg.INPUT_ROIS_PER_IMAGE, 5))

        all_gt_boxes = gt_data[np.where(gt_data[:, 4] != 0)]  # remove padded boxes!
        all_raw_gt_boxes.append(all_gt_boxes.copy())

        # TODO: remove ground truth from mb_data to avoid warning
        output = frcn_eval.eval(mb_data) #{image_input: mb_data[image_input], dims_input: mb_data[dims_input]})
        out_dict = dict([(k.name, k) for k in output])
        out_cls_pred = output[out_dict['']][0] # cls_pred
        out_rpn_rois = output[out_dict['rpn_rois']][0]
        out_bbox_regr = output[out_dict['bbox_regr']][0]

        #labels = out_cls_pred.argmax(axis=1)
        #import pdb; pdb.set_trace()
        #dims = mb_data[input_map[minibatch_source.dims_si]].asarray()
        #regressed_rois = regress_rois(out_rpn_rois, out_bbox_regr, labels, dims)
        regressed_rois = out_rpn_rois

        all_raw_outputs.append(out_cls_pred)
        all_raw_rois.append(regressed_rois)

        if img_i % 1000 == 0 and img_i != 0:
            print("Images processed: " + str(img_i))

    all_gt_infos = prepare_ground_truth_boxes(gtbs=all_raw_gt_boxes)
    all_boxes = prepare_predictions(all_raw_outputs, all_raw_rois, num_classes)

    aps = evaluate_detections(all_boxes, all_gt_infos, classes, use_gpu_nms=True, device_id=0, apply_mms=True, use_07_metric=True)
    ap_list = []
    for class_name in classes:
        if class_name == "__background__": continue
        ap_list += [aps[class_name]]
        print('AP for {:>15} = {:.6f}'.format(class_name, aps[class_name]))
    print('Mean AP = {:.6f}'.format(np.nanmean(ap_list)))

    return aps


img_list = [os.path.join(abs_path, r"../../DataSets/Grocery/testImages/WIN_20160803_11_28_42_Pro.jpg"),
            os.path.join(abs_path, r"../../DataSets/Grocery/testImages/WIN_20160803_11_42_36_Pro.jpg"),
            os.path.join(abs_path, r"../../DataSets/Grocery/testImages/WIN_20160803_11_46_03_Pro.jpg"),
            os.path.join(abs_path, r"../../DataSets/Grocery/testImages/WIN_20160803_11_48_26_Pro.jpg"),
            os.path.join(abs_path, r"../../DataSets/Grocery/testImages/WIN_20160803_12_37_07_Pro.jpg")]


# scramble to
def _scramble_list(to_sc, perm):
    out = []
    for i in perm:
        out.append(to_sc[i])
    return out


img_list = _scramble_list(img_list, [1, 3, 2, 4, 0])


def to_cv2_img(img):
    img.shape = (3,1000,1000)

    # CHW to HWC
    img = np.transpose(img, (1,2,0))
    img = np.asarray(img, dtype=np.uint8)

    return img


def visualize_gt(all_gt_infos, imgs=None, plot=True):
    if imgs is None:
        imgs = []
        for img_path in img_list:
            imgs.append(load_image(img_path))
        remove_padding = True
    else:
        # deep_copy
        imgs = [to_cv2_img(img.copy()) for img in imgs]
        remove_padding = False

    for cls_name in all_gt_infos:
        if cls_name == '__background__': continue
        pred_list = all_gt_infos[cls_name]
        if not len(pred_list) == len(imgs): import ipdb;ipdb.set_trace()
        for img_i in range(len(imgs)):
            image = imgs[img_i]
            pred = np.copy(pred_list[img_i]["bbox"])
            if image is None: import ipdb;ipdb.set_trace()
            add_rois_to_img(image, pred, cls_name, remove_padding)

    if plot:
        for img in imgs:
            plot_image(img)

    return imgs


def visualize_rois(all_boxes, imgs=None, plot=True):
    classes = HCH.output_mapper.get_all_classes()
    if imgs is None:
        imgs = []
        for img_path in img_list:
            imgs.append(load_image(img_path))
        remove_padding = True
    else:
        # deep_copy
        imgs = [img.copy() for img in imgs]
        remove_padding = False

    for cls_i in range(len(all_boxes)):

        cls_name = classes[cls_i]
        for img_i in range(len(imgs)):
            image = imgs[img_i]
            rois = np.copy(all_boxes[cls_i][img_i])

            add_rois_to_img(image, rois, cls_name, remove_padding)

    if plot:
        for img in imgs:
            plot_image(img)

    return imgs


def add_rois_to_img(img, rois, cls_name, remove_padding=True):
    if rois.size == 0: return

    rois[:, 0:4] /= output_scale + output_scale
    if(remove_padding):
        rois[:, [0, 2]] -= 7 / 32
        rois[:, [0, 2]] *= 16 / 9

    draw_bb_on_image(img, points_to_xywh(rois), cls_name)


def draw_bb_on_image(image, bb_list, name=None):
    import cv2
    image_width = len(image[1])
    image_height = len(image)

    LIMIT_TO_FIRST = None
    box_list_len = min(len(bb_list), LIMIT_TO_FIRST) if LIMIT_TO_FIRST is not None else len(bb_list)
    for j in range(box_list_len):
        box = bb_list[j]
        xmin = int(image_width * (box[0] - box[2] / 2))
        xmax = int(image_width * (box[0] + box[2] / 2))
        ymin = int(image_height * (box[1] - box[3] / 2))
        ymax = int(image_height * (box[1] + box[3] / 2))
        if (xmax >= image_width or ymax >= image_height or xmin < 0 or ymin < 0):
            print("Box out of bounds: (" + str(xmin) + "," + str(ymin) + ") (" + str(xmax) + "," + str(ymax) + ")")
            # print(box[5:])
        xmax = image_width - 1 if xmax >= image_width else xmax
        ymax = image_height - 1 if ymax >= image_height else ymax
        xmin = 0 if xmin < 0 else xmin
        ymin = 0 if ymin < 0 else ymin

        color = (255, 255 - int(j * 255 / box_list_len), int(j * 255 / box_list_len))
        cv2.rectangle(image, (xmin, ymin), (xmax, ymax), color, 1)

        if name is not None:
            cv2.putText(image, name, (xmin, ymax), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 1)

    return image


def plot_image(image):
    import matplotlib.pyplot as mp
    mp.imshow(image)
    mp.plot()
    mp.show()


def load_image(img_path):
    import cv2
    return cv2.imread(img_path)


def save_image(img, dir, name):
    import cv2
    cv2.imwrite(os.path.join(dir, name), img)


def points_to_xywh(points):
    xywh = np.zeros(points.shape)

    xywh[:, 0] = (points[:, 0] + points[:, 2]) / 2
    xywh[:, 1] = (points[:, 1] + points[:, 3]) / 2
    xywh[:, 2] = np.abs(points[:, 2] - points[:, 0])
    xywh[:, 3] = np.abs(points[:, 3] - points[:, 1])
    xywh[:, 4:] = points[:, 4:]

    return xywh



if __name__ == '__main__':
    """
    Evaluates the Classification of the model created by the H1 script. Since only the classification is to be tested
    the roi_input is given the ground truth boxes. This way it can be assured, that no issues due to bad region
    proposals is taken into account and the classification accurancy can be measured.
    """
    os.chdir(p.cntkFilesDir)
    model_path = os.path.join(abs_path, "Output", p.datasetName + "_hfrcn_py.model")

    # Train only if no model exists yet
    if os.path.exists(model_path):
        print("Loading existing model from %s" % model_path)
        trained_model = load_model(model_path)
    else:
        print("No trained model found! Start training now ...")
        import H1_RunHierarchical as h1

        trained_model = h1.create_and_save_model(model_path)
        print("Stored trained model at %s" % model_path)

    # Evaluate the test set
    eval_fast_rcnn_mAP(trained_model, p.cntk_num_test_images)


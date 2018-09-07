import os,time,random, rawpy
import tensorflow as tf
import numpy as np
import model.net as net
import utils as utils
import loss as losses
from PIL import Image
from shutil import copyfile
from tensorflow.core.protobuf import config_pb2

from multiprocessing import Process, Pool, Queue

is_debug = True
continue_training = True

mode = 'test'
substr = 'deconv_lum_x4_cont_coord_10'
task = 'restore_srresnet_deconv_lum_x4_cont_coord_10'  # restore_srresnet_l1 , restore_resnet
save_root = '/export/vcl-nfs2/shared/xuanerzh/zoom/'
restore_path = save_root + 'restore_srresnet_deconv_lum_x4_cont_coord_10'
upsample_type = 'deconv' # deconv, subpixel
loss_type = 'contextual'  #align # contextual # combine
align_loss_type = 'percep'
is_gt_gamma = False
file_type = 'RAW'
net_type = 'resnet' # unet resnet
opt_type = 'adam' # momentum # adam
num_channels = 64

num_in_ch = 4
num_out_ch = 1
batch_size = 1
tol = 16
up_ratio = 4
stride = 2
# "/export/vcl-nfs2/shared/xuanerzh/zoom/test/dslr_10x_both/00062/00006.ARW"
train_root = ['/export/vcl-nfs2/shared/xuanerzh/zoom/dslr_10x_both/dslr_10x_both/'] # path to raw files
test_root = ['/export/vcl-nfs2/shared/xuanerzh/zoom/test/dslr_10x_both/']
subfolder = 'dslr_10x_both'
maxepoch = 100
decay_step = 20000
decay_rate = 0.1
save_freq = 5000   # unit: step
save_img_freq = 400   #unit: step
save_summary_freq = 100  #unit: step

if loss_type == 'contextual':
    tol = 0
    w_align = 0
    w_smooth = 50
    w_cont = 1
    w_spatial = 0.7
elif loss_type == 'combine':
    tol = 16
    w_align = 100.
    w_cont = 1
    w_smooth = 10
    w_spatial = 0.0
elif loss_type == 'align':
    tol = 16
    w_align = 1
    w_cont = 1
    w_smooth = 0
    w_spatial = 0.0

raw_tol = 4
img_sz = 256
raw_sz = 32 + raw_tol*2

if not os.path.isdir("%s"%(task)):
    os.makedirs("%s"%(task))
    if mode == 'train':
        copyfile('./train.py', './%s/train_%s.py'%(task, time.strftime('%b-%d-%Y_%H%M', time.localtime())))
        copyfile('./model/net.py', './%s/net_%s.py'%(task, time.strftime('%b-%d-%Y_%H%M', time.localtime())))
        copyfile('./loss.py', './%s/loss_%s.py'%(task, time.strftime('%b-%d-%Y_%H%M', time.localtime())))
if not os.path.isdir("/home/xuanerzh/tmp_%s"%(substr)):
    os.makedirs("/home/xuanerzh/tmp_%s"%(substr))

### YUV2RGB
def yuv2rgb(yuv):
    """
    Convert YUV image into RGB https://en.wikipedia.org/wiki/YUV
    """
    yuv = tf.multiply(yuv, 255)
    yuv2rgb_filter = tf.constant(
        [[[[1., 1., 1.],
           [0., -0.34413999, 1.77199996],
            [1.40199995, -0.71414, 0.]]]])
    yuv2rgb_bias = tf.constant([-179.45599365, 135.45983887, -226.81599426])
    temp = tf.nn.conv2d(yuv, yuv2rgb_filter, [1, 1, 1, 1], 'SAME')
    temp = tf.nn.bias_add(temp, yuv2rgb_bias)
    temp = tf.maximum(temp, tf.zeros(tf.shape(temp), dtype=tf.float32))
    temp = tf.minimum(temp, tf.multiply(
        tf.ones(tf.shape(temp), dtype=tf.float32), 255))
    temp = tf.div(temp, 255)
    return temp

def rgb2yuv(rgb):
    """
    Convert RGB image into YUV https://en.wikipedia.org/wiki/YUV
    """
    rgb2yuv_filter = tf.constant(
        [[[[0.299, -0.169, 0.499],
           [0.587, -0.331, -0.418],
            [0.114, 0.499, -0.0813]]]])
    rgb2yuv_bias = tf.constant([0., 0.5, 0.5])

    temp = tf.nn.conv2d(rgb, rgb2yuv_filter, [1, 1, 1, 1], 'SAME')
    temp = tf.nn.bias_add(temp, rgb2yuv_bias)

    return temp

def gamma(rgb, gamma=2.2):
    rgb=tf.clip_by_value(rgb, 0., 1000)
    return rgb ** (1/gamma)

def degamma(rgb, gamma=2.2):
    rgb=tf.clip_by_value(rgb, 0., 1000)
    return rgb ** (gamma)

with tf.variable_scope(tf.get_variable_scope()):
    if 'inference' not in mode:
        input_raw=tf.placeholder(tf.float32,shape=[batch_size,raw_sz,raw_sz,num_in_ch], name="input_raw")
    else:
        input_raw=tf.placeholder(tf.float32,shape=[batch_size,None,None,num_in_ch], name="input_raw")
    # tar_shape = [1,256,256,3]
    # tar_w=tf.placeholder(tf.int32, shape=(), name="width")    
    
    if net_type == 'resnet':
        out_rgb = net.SRResnet(input_raw, num_out_ch, up_ratio=up_ratio, reuse=False, up_type=upsample_type, is_training=True)
    elif net_type == 'unet':
        out_rgb=net.build_unet(input_raw,
            channel=num_channels,
            input_channel=num_in_ch,
            output_channel=num_out_ch,
            reuse=False,
            num_layer=5,
            up_type=upsample_type)
    else:
        print("Unknown architecture type.")
        exit()

    if raw_tol != 0:
        out_rgb = out_rgb[:,int(raw_tol/2)*(up_ratio*4):-int(raw_tol/2)*(up_ratio*4),
            int(raw_tol/2)*(up_ratio*4):-int(raw_tol/2)*(up_ratio*4),:]  # add a small offset to deal with boudary case

    print("out_rgb shape:", out_rgb.shape)
    objDict = {}
    objDict['out_rgb'] = out_rgb
    # if NOT inference ---> means either test or train
    if 'inference' not in mode:
        target_rgb=tf.placeholder(tf.float32,shape=[batch_size,img_sz+tol*2,img_sz+tol*2,3], name="target_rgb")
        # tar_shape=tf.placeholder(tf.int32, shape=(4), name="height")
        tar_lum = rgb2yuv(target_rgb)[...,0,tf.newaxis]
        # out_rgb = tf.reshape(out_rgb, tar_shape)
        objDict['tar_lum'] = tar_lum

        loss_context = tf.constant(0.)
        loss_unalign = tf.constant(0.)
        loss_smooth = tf.constant(0.)
        target_translated = tar_lum
        # objDict['target_translated'] = target_translated
        if loss_type == 'align' or loss_type == 'combine':
            loss_unalign, target_translated=losses.compute_unalign_loss(out_rgb, tar_lum,
                tol=tol, losstype=align_loss_type, stride=stride)
            loss_unalign *= w_align
            if align_loss_type == 'percep':
                lossDict['percep'] = loss_unalign
            # loss_unalign += tf.reduce_mean(tf.abs(out_rgb-target_translated)) * 500
            tf.summary.scalar('loss_unalign', loss_unalign)
            objDict['target_translated'] = target_translated
    # if training
    if mode == 'train':
        if loss_type == 'contextual' or loss_type == 'combine':
            loss_unalign, target_translated=losses.compute_unalign_loss(out_rgb, tar_lum,
                tol=tol, losstype=align_loss_type, stride=stride)
            # target_translated = tf.reshape(target_translated, tar_shape)
            loss_context,loss_argmax = losses.compute_contextual_loss(out_rgb, target_translated, w_spatial=w_spatial)
            tf.summary.scalar('loss_context', loss_context)
        
        # loss_smooth = losses.compute_charbonnier_loss(out_rgb)
        # loss_smooth *= w_smooth
        # tf.summary.scalar('loss_smooth', loss_smooth)
        lossDict['l1'] = tf.reduce_mean(tf.abs(out_rgb-target_translated))
        lossDict['smooth'] = loss_smooth
        lossDict['loss_context'] = loss_context
        lossDict['l1_unalign'] = loss_unalign
        lossDict['total'] = loss_context* w_cont + loss_unalign*w_align + loss_smooth*w_smooth
    else:
        # do a match anyway
        loss_unalign, target_translated=losses.compute_unalign_loss(out_rgb, tar_lum,
            tol=tol, losstype=align_loss_type, stride=stride)
    objDict['target_translated'] = target_translated

if mode == "train":
    global_step = tf.train.get_or_create_global_step()
    learning_rate = tf.train.exponential_decay(0.0002, global_step, decay_step, decay_rate)
    if opt_type == 'momentum':
        opt = tf.train.MomentumOptimizer(learning_rate=0.00005, momentum=0.99).minimize(lossDict['total'],
            global_step=global_step,
            var_list=[var for var in tf.trainable_variables()])
    else:
        # learning_rate=tf.placeholder(tf.float32)
        opt=tf.train.AdamOptimizer(learning_rate=learning_rate).minimize(lossDict['total'],
            global_step=global_step,
            var_list=[var for var in tf.trainable_variables()])
    
    incr_global_step = tf.assign(global_step, global_step + 1)
    merged = tf.summary.merge_all()
    saver=tf.train.Saver(max_to_keep=10)

if mode == "train":
    opt=tf.train.AdamOptimizer(learning_rate=0.0001).minimize(loss_sum,
        var_list=[var for var in tf.trainable_variables()])

###################################### Session
sess=tf.Session()
merged = tf.summary.merge_all()
writer = tf.summary.FileWriter('%s/log'%(task), sess.graph)
if mode == 'train':
    saver_restore=tf.train.Saver([var for var in tf.trainable_variables()])
    #saver_restore=tf.train.Saver([var for var in tf.trainable_variables() if 'deconv_output_stage' not in var.name])
else:
    saver_restore=tf.train.Saver([var for var in tf.trainable_variables()])

sess.run(tf.global_variables_initializer())
ckpt=tf.train.get_checkpoint_state("%s"%(restore_path))
epoch_offset = 0

from tensorflow.python.tools.inspect_checkpoint import print_tensors_in_checkpoint_file
print_tensors_in_checkpoint_file(file_name='%s/model.ckpt'%(restore_path), tensor_name='', all_tensors=False)

print("contain checkpoint: ", ckpt)
if (not ckpt) and (mode != 'train' or continue_training):
    print("No checkpoint found.")
    exit()
if ckpt and continue_training:
    saver_restore.restore(sess,ckpt.model_checkpoint_path)
    try:
        epoch_offset = tf.train.get_checkpoint_state("%s"%(restore_path)).all_model_checkpoint_paths[-2]
        epoch_offset = int(os.path.basename(os.path.dirname(epoch_offset)))
    except Exception as exception:
        epoch_offset = 0
    print('loaded %s with epoch offset %d'%(ckpt.model_checkpoint_path, epoch_offset))

if mode == "train":

    def prepare_train_data(raw_path):
        tar_h_val = img_sz #np.random.randint(64, 72)*(2*up_ratio)
        tar_w_val = img_sz #np.random.randint(64, 72)*(2*up_ratio)
        if is_gt_gamma == True:
            rgb_path = os.path.dirname(raw_path).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                os.path.basename(raw_path).replace(".ARW","_gamma.png")
        else:
            rgb_path = os.path.dirname(raw_path).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                os.path.basename(raw_path).replace(".ARW",".png")
        id_shift=up_ratio-1 if up_ratio==4 else up_ratio
        input_dict = utils.read_input_2x(raw_path, rgb_path, id_shift=id_shift)
        if input_dict is None:
            print("input_dict is None ... for %s"%(raw_path))
            return None
        processed_dict = utils.prepare_input(input_dict, up_ratio=up_ratio, is_pack=True)
        input_raw_img_orig,target_rgb_img_orig = processed_dict['input_raw'], processed_dict['tar_rgb']
        # print("Read in image shapes:",train_input_paths[id], input_raw_img_orig.shape, target_rgb_img_orig.shape)
        if input_raw_img_orig is None or target_rgb_img_orig is None:
            print('Invalid input raw or rgb for %s'%(raw_path))
            return None

        # prepare input to pre-align
        row, col = input_raw_img_orig.shape[0:2]
        target_rgb_img_orig, transformed_corner = utils.post_process_rgb(target_rgb_img_orig,
            (int(col*2*up_ratio),int(row*2*up_ratio)), processed_dict['tform'])
        input_raw_img_orig = input_raw_img_orig[int(transformed_corner['minw']/(2*up_ratio)):int(transformed_corner['maxw']/(2*up_ratio)),
            int(transformed_corner['minh']/(2*up_ratio)):int(transformed_corner['maxh']/(2*up_ratio)),:]
        cropped_raw, cropped_rgb = utils.crop_pair(input_raw_img_orig, target_rgb_img_orig, 
            croph=tar_h_val, cropw=tar_w_val, tol=tol, ratio=up_ratio, type='central')
        
        cropped = {}
        cropped['cropped_raw'] = cropped_raw
        cropped['cropped_rgb'] = cropped_rgb
        cropped['input_dict'] = input_dict

        return cropped
    
    def read_images(input_queue, files):
        for file in files:
            input_queue.put(prepare_train_data(file))

    train_input_paths=utils.read_paths(train_root, type=file_type)
    train_input_paths=train_input_paths
    num_train_per_epoch=len(train_input_paths)
    train_input_paths = train_input_paths*maxepoch
    num_train = len(train_input_paths)
    print("Number of training images: ", num_train_per_epoch)
    print("Total %d raw images" % (num_train_per_epoch))
    all_loss=np.zeros(num_train, dtype=float)
    cnt=0
    epoch = 0

    train_input_paths_permute = list(np.random.permutation(train_input_paths))
    num_process = 8
    # num_train_per_process = num_train // num_process
    input_queue = Queue(32)
    for i in range(num_process):
        producer = Process(target=read_images, args=(input_queue, train_input_paths_permute[i::num_process]))
        producer.start()

    for id in range(num_train):
        if id % num_train_per_epoch == 0:
            epoch += 1
            print("Processing epoch %d"%(epoch))
        
        if os.path.isdir("%s/%04d"%(task,epoch)):
            continue
        
        # save_img_freq = 500 if epoch >= 1+epoch_offset+3 else 200 # save less frequently after third epoch
        
        # input_raw_img=[None]*num_train
        # target_rgb_img=[None]*num_train
        # for id in range(num_train):

        # print(input_queue.qsize())
        cropped = input_queue.get()

        if cropped is None:
            continue
        
        cropped_raw = cropped['cropped_raw']
        cropped_rgb = cropped['cropped_rgb']
        input_dict = cropped['input_dict']

        if cropped_raw is None or cropped_rgb is None:
            continue

        if cropped_raw.shape[0] * 2 * up_ratio + tol * 2 != cropped_rgb.shape[0]:
            print("xxxxx Wrong cropped shapes.")
            continue
        
        target_rgb_img = np.expand_dims(cropped_rgb, 0)
        input_raw_img = np.expand_dims(cropped_raw, 0)
        print("Processed image shapes: ", input_raw_img.shape, target_rgb_img.shape)
        
        fetch_list=[opt,learning_rate,global_step,merged,objDict,lossDict,loss_argmax]
        st=time.time()
        _, out_lr, out_global_step, out_summary,out_objDict,out_lossDict,out_arg=sess.run(fetch_list,feed_dict=
            {input_raw:input_raw_img,
            target_rgb:target_rgb_img})
            # tar_shape:[1,tar_h_val,tar_w_val,num_out_ch]})
            # learning_rate:min(1e-5*np.power(1.1,epoch-1),1e-5)}) #learning_rate:min(1e-5*np.power(1.1,epoch-1),1e-4)})
        all_loss[id]=out_lossDict["total"]

        cnt+=1
        
        print("%s --- lr: %.5f --- Global_step: %d" % (task,out_lr, out_global_step))
        print("Iter: %d %d || loss: (t) %.4f (l1) %.4f (l_cont) %.4f (l_smooth) %.4f || mean: %.4f || time: %.2f"%
            (epoch,cnt,out_lossDict["total"],
                out_lossDict["l1"],
                out_lossDict["loss_context"],
                out_lossDict['smooth'],
                np.mean(all_loss[np.where(all_loss)]),
                time.time()-st))
        for i in range(3):
            print("argmax of feature %d"%(i), len(np.unique(out_arg[i])))
        
        if cnt % save_summary_freq == 0:
            writer.add_summary(out_summary, cnt)
        if is_debug and (cnt-1) % save_img_freq == 0:
            cropped_raw_reshaped = utils.reshape_back_raw(cropped_raw)
            cropped_raw_rgb = utils.write_raw(cropped_raw_reshaped, input_dict['src_path_raw'])
            cropped_raw_rgb = cropped_raw_rgb[:cropped_raw_reshaped.shape[0],:cropped_raw_reshaped.shape[1]]
            input_rgb_cropped = Image.fromarray(np.uint8(
                utils.clipped(utils.apply_gamma(cropped_raw_rgb))*255))
            input_rgb_cropped = input_rgb_cropped.resize((int(input_rgb_cropped.width * up_ratio),
                int(input_rgb_cropped.height * up_ratio)), Image.ANTIALIAS)
            input_rgb_cropped.save("/home/xuanerzh/tmp_%s/input_rgb_cropped_%d_%d.png"%(substr, epoch, cnt))

            output_rgb = utils.apply_gamma(
                utils.clipped(np.squeeze(out_objDict["out_rgb"][0,...])),is_apply=is_gt_gamma==False)*255
            src_raw = Image.fromarray(np.uint8(utils.clipped(utils.apply_gamma(input_raw_img[0,...,0]))*255))
            tartget_rgb = Image.fromarray(np.uint8(utils.clipped(
                utils.apply_gamma(target_rgb_img[0,...], is_apply=is_gt_gamma==False))*255))
            Image.fromarray(np.uint8(output_rgb)).save("/home/xuanerzh/tmp_%s/out_rgb_%d_%d.png"%(substr, epoch, cnt))
            src_raw.save("/home/xuanerzh/tmp_%s/src_raw_%d_%d.png"%(substr, epoch, cnt))
            tartget_rgb.save("/home/xuanerzh/tmp_%s/tar_rgb_%d_%d.png"%(substr, epoch, cnt))
            if loss_type == 'align' or loss_type == 'combine':
                gt_match = Image.fromarray(np.uint8(utils.clipped(utils.apply_gamma(np.squeeze(
                    out_objDict['target_translated']),is_apply=is_gt_gamma==False))*255))
                gt_match.save("/home/xuanerzh/tmp_%s/tar_rgb_match_%d_%d.png"%(substr, epoch, cnt))

            target_buffer = target_rgb_img
            # input_raw_img[id]=1.
            # target_rgb_img[id]=1.

        if cnt % save_freq == 0:
            print("saving model ...")
            if not os.path.isdir("%s%s/%04d"%(save_root,task,epoch)):
                os.makedirs("%s%s/%04d"%(save_root,task,epoch))
            saver.save(sess,"%s%s/model.ckpt"%(save_root,task))
            saver.save(sess,"%s%s/%04d/model.ckpt"%(save_root,task,epoch))
            try:
                output_rgb = utils.clipped(np.squeeze(out_objDict["out_rgb"][0,...]))*255
                output_rgb = Image.fromarray(np.uint8(output_rgb))
                tartget_rgb = utils.clipped(utils.apply_gamma(target_buffer[0,...]))*255
                tartget_rgb = Image.fromarray(np.uint8(tartget_rgb))
                output_rgb.save("%s%s/%04d/out_rgb_%s.png"%(save_root,task,epoch,substr))
                tartget_rgb.save("%s%s/%04d/tar_rgb_%s.png"%(save_root,task,epoch,substr))
                if loss_type == 'align' or loss_type == 'combine':
                    gt_match = Image.fromarray(np.uint8(utils.clipped(utils.apply_gamma(np.squeeze(out_objDict['target_translated'])))*255))
                    gt_match.save("%s%s/%04d/tar_rgb_match_%s.png"%(save_root,task,epoch,substr))
            except Exception as exception:
                print("Failed to write image footprint. ;(")
        # producer.join()

elif mode == 'test':
    def compute_psnr(ref, target):
        ref = np.float32(np.array(ref))
        target = np.float32(np.array(target))
        diff = target - ref
        sqr = np.multiply(diff, diff)
        err = np.sum(sqr)
        v = np.prod(list(diff.shape))
        mse = err / np.float32(v)
        psnr = 10. * (np.log(255. * 255. / mse) / np.log(10.))
        return psnr

    test_input_paths=utils.read_paths(test_root, type=file_type)
    
    # test_input_paths = ['/export/vcl-nfs2/shared/xuanerzh/zoom/test/dslr_10x_both/00286/00007.ARW']

    num_test=len(test_input_paths)
    # num_test = 50
    print("Total %d test images."%(num_test))
    test_folder = 'test'
    if not os.path.isdir("%s/%s"%(task, test_folder)):
        os.makedirs("%s/%s"%(task, test_folder))
    result=open("%s/%s/score.txt"%(task,test_folder),'w')
    psnr_input = np.empty((num_test,))
    psnr_output = np.empty((num_test,))
    for id in range(num_test):
        ct = id
        print("Testing on %d th image : %s"%(id, test_input_paths[id]))
        
        if is_gt_gamma == True:
            rgb_path = os.path.dirname(test_input_paths[id]).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                os.path.basename(test_input_paths[id]).replace(".ARW","_gamma.png")
        else:
            rgb_path = os.path.dirname(test_input_paths[id]).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                os.path.basename(test_input_paths[id]).replace(".ARW",".png")
        
        print(rgb_path)
        input_dict = utils.read_input_2x(test_input_paths[id], rgb_path, id_shift=up_ratio, is_training=mode=='train')
        if input_dict is None:
            print("input_dict is None ...")
            continue
        processed_dict = utils.prepare_input(input_dict, up_ratio=up_ratio, is_pack=True)
        print("raw path", input_dict['src_path_raw'], input_dict['tar_path'])
        input_raw_img_orig,target_rgb_img_orig = processed_dict['input_raw'], processed_dict['tar_rgb']
        input_rgb_img_orig = processed_dict['input_rgb']
        # print("Read in image shapes:",test_input_paths[id], input_raw_img_orig.shape, target_rgb_img_orig.shape)
        if input_raw_img_orig is None or target_rgb_img_orig is None:
            print('Invalid input raw or rgb for %s'%(test_input_paths[id]))
            continue

        # prepare input to pre-align
        row, col = input_raw_img_orig.shape[0:2]
        target_rgb_img_orig, transformed_corner = utils.post_process_rgb(target_rgb_img_orig,
            (int(col*2*up_ratio),int(row*2*up_ratio)), processed_dict['tform'])
        input_raw_img_orig = input_raw_img_orig[int(transformed_corner['minw']/(2*up_ratio)):int(transformed_corner['maxw']/(2*up_ratio)),
            int(transformed_corner['minh']/(2*up_ratio)):int(transformed_corner['maxh']/(2*up_ratio)),:]
        
        input_rgb_img_orig = input_rgb_img_orig[int(transformed_corner['minw']/(up_ratio)):int(transformed_corner['maxw']/(up_ratio)),
            int(transformed_corner['minh']/(up_ratio)):int(transformed_corner['maxh']/(up_ratio))]

        # tar_h_val = np.random.randint(32, 64)*(2*up_ratio)
        # tar_w_val = np.random.randint(32, 64)*(2*up_ratio)
        tar_h_val = img_sz
        tar_w_val = img_sz
        cropped_raw, cropped_rgb = utils.crop_pair(input_raw_img_orig, target_rgb_img_orig, 
            croph=tar_h_val, cropw=tar_w_val, tol=tol, ratio=up_ratio, type='fixed', fixx=0.5, fixy=0.5)
        
        if cropped_raw is None or cropped_rgb is None:
            print("cropped_raw or cropped_rgb is None ... ")
            continue

        cropped_raw_reshaped = utils.reshape_back_raw(cropped_raw)
        cropped_raw_rgb = utils.write_raw(cropped_raw_reshaped, input_dict['src_path_raw'])
        print(cropped_raw_reshaped.shape, cropped_rgb.shape, cropped_raw_rgb.shape)

        cropped_raw_rgb = cropped_raw_rgb[raw_tol*2:cropped_raw_reshaped.shape[0]-raw_tol*2,
            raw_tol*2:cropped_raw_reshaped.shape[1]-raw_tol*2]

        # print("cropped_raw_rgb range:", cropped_rgb.min(), cropped_rgb.max())

        target_rgb_img = np.expand_dims(cropped_rgb, 0)
        input_raw_img = np.expand_dims(cropped_raw, 0)
        
        print("Input shapes: ", input_raw_img.shape, target_rgb_img.shape)

        # fetch_list=[objDict]
        out_objDict=sess.run(objDict,feed_dict=
            {input_raw:input_raw_img,
            target_rgb:target_rgb_img})

        print("out rgb: ", out_objDict["out_rgb"][0,...].shape)

        # argmin = np.squeeze(out_objDict['argmin'])
        # argminx, argminy = np.unravel_index(argmin, (tol,tol))
        # print("Found best match pos:", argminx, argminy)
        # translation_matrix = np.float32([[1,0,argminx*stride], [0,1,argminy*stride]])
        # target_rgb_match = utils.apply_transform_single(cropped_rgb, translation_matrix, tar_h, tar_w)
        
        if not os.path.isdir("%s/%s/%d"%(task, test_folder, id)):
            os.makedirs("%s/%s/%d"%(task, test_folder, id))
        gt_match = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(np.squeeze(out_objDict['target_translated']),is_apply=is_gt_gamma==False))*255))
        output_rgb = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(np.squeeze(out_objDict["out_rgb"][0,...]),is_apply=is_gt_gamma==False))*255))
        gt_rgb = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(cropped_rgb,is_apply=is_gt_gamma==False))*255))
        gt_rgb_cropped = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(cropped_rgb[tol:tol+tar_h_val,tol:tol+tar_w_val,:],is_apply=is_gt_gamma==False))*255))
        input_rgb = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(input_rgb_img_orig,is_apply=is_gt_gamma==False))*255))
        input_rgb_cropped = Image.fromarray(np.uint8(utils.clipped(
            utils.apply_gamma(cropped_raw_rgb,is_apply=False))))
        # print("input_rgb_cropped", input_rgb_cropped)
        input_rgb_cropped.save("%s/%s/%d/input_rgb_cropped_orig.png"%(task,test_folder,id))
        input_rgb_cropped = input_rgb_cropped.resize((int(input_rgb_cropped.width * up_ratio),
                    int(input_rgb_cropped.height * up_ratio)), Image.ANTIALIAS)
        if 'lum' in task:
            gt_match = gt_match.convert('L')
            input_rgb_cropped = input_rgb_cropped.convert('L')
        psnr_input[ct] = compute_psnr(gt_match, input_rgb_cropped)
        psnr_output[ct] = compute_psnr(gt_match, output_rgb)
        print("PSNR of input is %s and PSNR of output is %s."%(psnr_input[ct], psnr_output[ct]))

        result.write("test %d:%s: %f, %f\n"%(ct, test_input_paths[id], psnr_input[ct], psnr_output[ct]))
        
        input_rgb.save("%s/%s/%d/input_rgb.png"%(task,test_folder,id))
        input_rgb_cropped.save("%s/%s/%d/input_rgb_cropped.png"%(task,test_folder,id))
        output_rgb.save("%s/%s/%d/out_rgb.png"%(task,test_folder,id))
        gt_rgb.save("%s/%s/%d/tar_rgb.png"%(task,test_folder,id))
        gt_rgb_cropped.save("%s/%s/%d/tar_rgb_cropped.png"%(task,test_folder,id))
        gt_match.save("%s/%s/%d/tar_rgb_match.png"%(task,test_folder,id))
    result.write("test mean: %f, %f\n"%(np.nanmean(psnr_input), np.nanmean(psnr_output)))
    result.close()

elif mode == 'inference':
        test_input_paths=utils.read_paths(test_root, type=file_type)
        
        inference_folder = 'inference'
        if not os.path.isdir("%s/%s"%(task, inference_folder)):
            os.makedirs("%s/%s"%(task, inference_folder))
        
        inference_path = None
        # inference_path = "/export/vcl-nfs2/shared/xuanerzh/zoom/dslr_10x_both/dslr_10x_both/00011/00004.ARW"
        if inference_path is None:
            num_test = len(test_input_paths)
        else:
            num_test=1
            test_input_paths[0] = inference_path

        for id in range(num_test):
            inference_path = test_input_paths[id]

            if is_gt_gamma == True:
                rgb_path = os.path.dirname(test_input_paths[id]).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                    os.path.basename(inference_path).replace(".ARW","_gamma.png")
            else:
                rgb_path = os.path.dirname(test_input_paths[id]).replace(subfolder, subfolder+'_process') + "/rawpng/" + \
                    os.path.basename(inference_path).replace(".ARW",".png")

            input_dict = utils.read_input_2x(inference_path, rgb_path, is_training=False, id_shift=up_ratio)
            if input_dict is None:
                continue
            print("Testing on %d th image : %s"%(id, inference_path))
            print(input_dict['src_path_raw'])
            
            processed_dict = utils.prepare_input(input_dict, up_ratio=up_ratio, mode='inference')
            input_raw_img_orig = processed_dict['input_raw']
            input_raw_orig = rawpy.imread(input_dict['src_path_raw'])
            input_rgb_img_orig = input_raw_orig.postprocess(no_auto_bright=True,use_camera_wb=False,output_bps=8)
            row, col = input_raw_img_orig.shape[0:2]
            #tar_rgb = Image.open(os.path.dirname(input_dict['tar_path'])+'/'+os.path.basename(input_dict['tar_path'].split('.')[0]+'.png'))
            #tar_rgb_orig = utils.crop_fov(np.array(tar_rgb), 1./input_dict['ratio_ref1'])
            #target_rgb_img_orig, transformed_corner = utils.post_process_rgb(tar_rgb_orig,
            #        (int(col*2*up_ratio),int(row*2*up_ratio)), processed_dict['tform'])
            input_rgb_img_orig = utils.crop_fov(input_rgb_img_orig, 1./input_dict['ratio_ref2'])
            if input_raw_img_orig is None:
                print('Invalid input raw or rgb for %s'%(inference_path))
                continue

            # prepare input to pre-align
            
            input_raw_img = np.expand_dims(input_raw_img_orig, 0)

            print("Processed image raw shapes: ", input_raw_img.shape)
            out_objDict=sess.run(objDict,feed_dict={input_raw:input_raw_img},
                options=config_pb2.RunOptions(report_tensor_allocations_upon_oom=True))
            
            print("Finish inference ... ")
            if not os.path.isdir("%s/%s/%d"%(task, inference_folder, id)):
                os.makedirs("%s/%s/%d"%(task, inference_folder, id))
            output_rgb = Image.fromarray(np.uint8(
                utils.apply_gamma(utils.clipped(np.squeeze(out_objDict["out_rgb"][0,...])),is_apply=is_gt_gamma==False)*255))
            input_rgb = Image.fromarray(input_rgb_img_orig)
            input_rgb.save("%s/%s/%d/input_rgb.png"%(task,inference_folder,id))
            input_rgb_naive = input_rgb.resize((int(input_rgb.width * up_ratio),
                int(input_rgb.height * up_ratio)), Image.ANTIALIAS)
            input_rgb_naive.save("%s/%s/%d/input_rgb_naive.png"%(task,inference_folder,id))
            output_rgb.save("%s/%s/%d/out_rgb.png"%(task,inference_folder,id))

elif mode == 'inference_single':
        test_input_paths=utils.read_paths(test_root, type=file_type)
        up_ratio = 6.8
        scale_fov = 6.8

        inference_folder = 'inference_single'
        if not os.path.isdir("%s/%s"%(task, inference_folder)):
            os.makedirs("%s/%s"%(task, inference_folder))
        
        inference_path = "/export/vcl-nfs2/shared/xuanerzh/zoom/test/dslr_10x_both/00062/00006.ARW"
        id = 247
        inference_rgb_path = "/export/vcl-nfs2/shared/xuanerzh/zoom/test/dslr_10x_both_process/00062/rawpng/00006.png"

        input_bayer = utils.get_bayer(inference_path)
        input_raw_reshape = utils.reshape_raw(input_bayer)
        input_raw_img_orig = utils.crop_fov(input_raw_reshape, 1./up_ratio)
        
        input_raw_rawpy = rawpy.imread(inference_path)
        input_rgb_rawpy = input_raw_rawpy.postprocess(no_auto_bright=True,use_camera_wb=False,output_bps=8)
        cropped_input_rgb_rawpy = utils.crop_fov(input_rgb_rawpy, 1./up_ratio)
        # cropped_input_rgb_rawpy = utils.image_float(cropped_input_rgb_rawpy)

        rgb_camera_path = inference_path.replace(".ARW",".JPG")
        rgb_camera =  np.array(Image.open(rgb_camera_path))
        cropped_input_rgb = utils.crop_fov(rgb_camera, 1./up_ratio)
        cropped_input_rgb = utils.image_float(cropped_input_rgb)

        print("Testing on image : %s"%(inference_path), input_raw_img_orig.shape)

        # prepare input to pre-align
        row, col = input_raw_img_orig.shape[0:2]
        input_raw_img = np.expand_dims(input_raw_img_orig, 0)
        size = min(row, col) * 0.4
        # input_raw_img = np.expand_dims(input_raw_img_orig[int(row*0.15):int(row*0.15+size), int(col*0.4):int(col*0.4+size), :], 0)

        # out_objDict=sess.run(objDict,feed_dict={input_raw:input_raw_img})
        out_objDict=sess.run(objDict,feed_dict={input_raw:input_raw_img})
        
        print("Finish inference ... ")
        if not os.path.isdir("%s/%s/%d"%(task, inference_folder, id)):
            os.makedirs("%s/%s/%d"%(task, inference_folder, id))
        output_rgb = Image.fromarray(np.uint8(utils.apply_gamma(utils.clipped(out_objDict["out_rgb"][0,...]),is_apply=is_gt_gamma==False)*255))
        input_camera_rgb = Image.fromarray(np.uint8(utils.clipped(cropped_input_rgb)*255))
        input_rawpy_rgb = Image.fromarray(np.uint8(cropped_input_rgb_rawpy))
        input_camera_rgb_naive = input_camera_rgb.resize((int(input_camera_rgb.width * up_ratio),
            int(input_camera_rgb.height * up_ratio)), Image.ANTIALIAS)
        input_rawpy_rgb = input_rawpy_rgb.resize((int(input_rawpy_rgb.width * up_ratio),
            int(input_rawpy_rgb.height * up_ratio)), Image.ANTIALIAS)
        input_camera_rgb_naive.save("%s/%s/%d/input_rgb_camera_naive.png"%(task,inference_folder,id))
        input_rawpy_rgb.save("%s/%s/%d/input_rgb_rawpy_naive.png"%(task,inference_folder,id))
        output_rgb.save("%s/%s/%d/out_rgb.png"%(task,inference_folder,id))

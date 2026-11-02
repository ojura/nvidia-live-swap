/*
 * SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: MIT
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
 * DEALINGS IN THE SOFTWARE.
 */

/* Expected dispatcher body for compatibility checks; not compiled. */
void
gpuNotifySubDeviceEvent_IMPL
(
    OBJGPU *pGpu,
    NvU32   notifyIndex,
    void   *pNotifyParams,
    NvU32   notifyParamsSize,
    NvV32   info32,
    NvV16   info16
)
{
    PEVENTNOTIFICATION pEventNotification;
    THREAD_STATE_NODE *pCurThread;
    NvU32 localNotifyType;
    NvU32 localInfo32;
    NvU32 i;

    if (NV_OK == threadStateGetCurrent(&pCurThread, pGpu))
    {


        NV_ASSERT_OR_RETURN_VOID(!(pCurThread->flags & THREAD_STATE_FLAGS_IS_ISR_LOCKLESS));
    }

    NV_ASSERT_OR_RETURN_VOID(notifyIndex < NV2080_NOTIFIERS_MAXCOUNT);


    for (i = 0; i < pGpu->numSubdeviceBackReferences; i++)
    {
        Subdevice *pSubdevice;
        INotifier *pNotifier;


        portSyncSpinlockAcquire(pGpu->pSubdeviceBackReferencesLock);
        if (i >= pGpu->numSubdeviceBackReferences)
        {

            portSyncSpinlockRelease(pGpu->pSubdeviceBackReferencesLock);
            continue;
        }
        pSubdevice = pGpu->pSubdeviceBackReferences[i];
        if (pSubdevice != NULL)
        {
            portAtomicIncrementU32(&pSubdevice->notificationRefCount);
        }
        portSyncSpinlockRelease(pGpu->pSubdeviceBackReferencesLock);

        if (pSubdevice == NULL)
            continue;

        pNotifier = staticCast(pSubdevice, INotifier);

        if (inotifyGetNotificationShare(pNotifier) == NULL)
        {
            portAtomicDecrementU32(&pSubdevice->notificationRefCount);
            continue;
        }

        GPU_RES_SET_THREAD_BC_STATE(pSubdevice);






        localNotifyType = notifyIndex;
        localInfo32 = info32;

        {
            NvU16 partitionAttributionId = info16;
            if (_gpuFilterSubDeviceEventInfo(pGpu, pSubdevice,
                                             &localNotifyType,
                                             &localInfo32,
                                             partitionAttributionId) != NV_OK)
            {
                portAtomicDecrementU32(&pSubdevice->notificationRefCount);
                continue;
            }
        }

        if (pSubdevice->notifyActions[localNotifyType] == NV2080_CTRL_EVENT_SET_NOTIFICATION_ACTION_DISABLE)
        {
            portAtomicDecrementU32(&pSubdevice->notificationRefCount);
            continue;
        }

        if (pSubdevice->hNotifierMemory != NV01_NULL_OBJECT &&
            pSubdevice->pNotifierMemory != NULL)
        {
            notifyFillNotifierMemory(pGpu, pSubdevice->pNotifierMemory, localInfo32, info16,
                NV2080_SUBDEVICE_NOTIFICATION_STATUS_DONE_SUCCESS, localNotifyType);
        }

        pEventNotification = inotifyGetNotificationList(pNotifier);
        if (pEventNotification != NULL)
        {

            osEventNotificationWithInfo(pGpu, pEventNotification, localNotifyType, localInfo32, info16,
                                        pNotifyParams, notifyParamsSize);
        }


        if (pSubdevice->notifyActions[localNotifyType] == NV2080_CTRL_EVENT_SET_NOTIFICATION_ACTION_SINGLE)
        {
            pSubdevice->notifyActions[localNotifyType] = NV2080_CTRL_EVENT_SET_NOTIFICATION_ACTION_DISABLE;
        }

        portAtomicDecrementU32(&pSubdevice->notificationRefCount);
    }
}
